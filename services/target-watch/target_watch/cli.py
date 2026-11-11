"""The `target-watch` command: match | sink | gmail-poll | replay | check | status."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import paths
from .log import log


def _since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--since needs a timezone, e.g. 2026-11-04T00:00:00Z")
    return parsed.astimezone(UTC)


def _read_events(path: str) -> list[dict]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    text = text.strip()
    if not text:
        return []
    if text[0] == "[":
        return json.loads(text)
    if text[0] == "{":
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            pass
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _registry(args):
    from .targets import TargetRegistry

    return TargetRegistry(paths.target_globs(args.targets), log=log)


def _index(args):
    from .xsender import XSenderIndex

    return XSenderIndex(Path(args.schemas_dir) if args.schemas_dir else paths.schemas_dir(), log=log)


def _state(args, *, memory: bool = False, read_only: bool = False):
    from .state import State

    if memory:
        return State(":memory:")
    root = Path(args.state_dir).expanduser() if args.state_dir else paths.state_dir()
    return State(root / "state.db", read_only=read_only)


async def _run_consumer(args, service, *, name: str, durable: str, subjects: list[str], ack_wait: float):
    from . import bus

    if args.from_file:  # dry run only: the live --from-file path connects in its caller
        for event in _read_events(args.from_file):
            await service.handle(event)
        return 0
    nc = await bus.connect(f"target-watch-{name}", args.nats_url)
    try:
        js = nc.jetstream()
        if not args.dry_run:
            service.publisher = bus.Publisher(js)
        sub = await bus.subscribe(
            js, None if args.dry_run else durable, subjects, since=_since(args.since), ack_wait=ack_wait,
        )
        stop = bus.install_stop(asyncio.get_running_loop())
        log(f"{name}_started", durable=None if args.dry_run else durable, subjects=subjects, dry_run=args.dry_run)
        await bus.consume(sub, service.handle, name=name, stop=stop, once=args.once or args.dry_run)
    finally:
        await nc.drain()
    return 0


async def cmd_match(args) -> int:
    from . import bus
    from .match_service import DURABLE, SUBJECTS, MatchService

    publisher = bus.DryRunPublisher() if args.dry_run else None
    service = MatchService(_index(args), _registry(args), _state(args, memory=args.dry_run), publisher)
    if args.from_file and not args.dry_run:
        nc = await bus.connect("target-watch-match", args.nats_url)
        try:
            service.publisher = bus.Publisher(nc.jetstream())
            for event in _read_events(args.from_file):
                await service.handle(event)
        finally:
            await nc.drain()
        return 0
    return await _run_consumer(args, service, name="match", durable=DURABLE, subjects=SUBJECTS, ack_wait=60)


async def cmd_sink(args) -> int:
    from . import bus
    from .sink import SinkService

    px = os.environ.get("TARGET_WATCH_PX", "px")
    hermes = os.environ.get("TARGET_WATCH_HERMES", "hermes")
    publisher = bus.DryRunPublisher() if args.dry_run else None
    service = SinkService(_registry(args), _state(args, read_only=args.dry_run), publisher,
                          px=px, hermes=hermes, dry_run=args.dry_run)
    if args.from_file and not args.dry_run:
        nc = await bus.connect("target-watch-sink", args.nats_url)
        try:
            service.publisher = bus.Publisher(nc.jetstream())
            for event in _read_events(args.from_file):
                await service.handle(event)
        finally:
            await nc.drain()
        return 0
    return await _run_consumer(args, service, name="sink", durable="target-watch-sink",
                               subjects=["bloodbank.evt.watch.target.detected"], ack_wait=300)


async def cmd_gmail_poll(args) -> int:
    from . import bus
    from .gmail import Gog, poll
    from .sink import run_command
    from .targets import load_targets

    targets, errors = load_targets(paths.target_globs(args.targets))
    for error in errors:
        log("target_error", error=error)
    gog = Gog(run_command, account=args.account)
    state = _state(args, read_only=args.dry_run)
    try:
        if args.dry_run:
            summary = await poll(targets, gog, state, bus.DryRunPublisher(), dry_run=True)
        else:
            nc = await bus.connect("target-watch-gmail-poll", args.nats_url)
            try:
                summary = await poll(targets, gog, state, bus.Publisher(nc.jetstream()))
            finally:
                await nc.drain()
    except Exception as exc:  # noqa: BLE001 - one line, then a non-zero exit for Candybar's on_failure
        print(f"gmail-poll failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    print("gmail-poll: " + " ".join(f"{k}={v}" for k, v in summary.items()) + (" (dry run)" if args.dry_run else ""))
    return 1 if summary["failed"] else 0


def cmd_replay(args) -> int:
    from .replay import fetch_events, replay

    since = _since(args.since)
    registry = _registry(args)
    events = fetch_events(since.isoformat().replace("+00:00", "Z"), base_url=args.candystore)
    rows = replay(events, _index(args), registry.current(), target_id=args.target)
    if args.json:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    else:
        print(f"{len(events)} messaging/telephony events since {args.since}; {len(rows)} detections")
        for row in rows:
            text = " ".join(row["text"].split())
            text = text if len(text) <= 50 else text[:49] + "…"
            dup = " (near-duplicate, dropped)" if row["near_duplicate"] else ""
            group = f" [{row['group_label']}]" if row["group_label"] else ""
            print(f"{row['received_at']}  {row['watch_id']}  {row['provider']:<10} {row['matched_on']:<13}{group} {text}{dup}")
    return 0


def cmd_check(args) -> int:
    from .sink import mask_channel
    from .targets import load_targets
    from .xsender import scan

    failures = 0
    globs = paths.target_globs(args.targets)
    targets, errors = load_targets(globs)
    print("targets (globs: " + ", ".join(globs) + ")")
    for t in targets:
        sink = t.sink
        where = f"repo={sink.repo}" if sink.kind == "plane" else ""
        problems = []
        if sink.kind == "plane" and not (sink.repo and (sink.repo / ".project.json").is_file()):
            problems.append("sink repo has no .project.json (px cannot bind a board)")
            failures += 1
        print(f"  {t.id:<20} {t.display_name:<24} email={len(t.email)} e164={len(t.e164)} label={len(t.label)} "
              f"platform={len(t.platform)}  sink={sink.kind} {where} notify={','.join(mask_channel(c) for c in sink.notify) or '-'}  {t.path}")
        for problem in problems:
            print(f"    ! {problem}")
    for error in errors:
        print(f"  ! {error}")
    failures += len(errors)
    root = Path(args.schemas_dir) if args.schemas_dir else paths.schemas_dir()
    index, xs_errors = scan(root)
    print(f"x-sender index ({root}): {len(index)} inbound types")
    for ce_type, xs in sorted(index.items()):
        inbound = f"{xs.inbound_path} in {list(xs.inbound_equals)}" if xs.inbound_path else "inbound by type"
        print(f"  {ce_type:<46} kinds={','.join(xs.handle_kinds):<22} handle={','.join(xs.handle_paths):<20} {inbound}")
    for error in xs_errors:
        print(f"  ! {error}")
    failures += len(xs_errors)
    if not index:
        print("  ! no schema carries x-sender: nothing can be matched")
        failures += 1
    if not targets:
        print("  ! no targets loaded")
        failures += 1
    print("ok" if not failures else f"{failures} problem(s)")
    return 0 if not failures else 1


def cmd_status(args) -> int:
    from .status import render

    state = _state(args, read_only=True)
    routes = state.recent_routes(args.limit, args.target)
    print(f"last {len(routes)} detection(s), newest first" + (f", target {args.target}" if args.target else ""))
    print(render(routes))
    return 0


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="target-watch", description="Target-focused alerts over every inbound provider.")
    p.add_argument("--targets", help="colon-separated target globs (default: $TARGET_WATCH_PATHS or the built-in set)")
    p.add_argument("--schemas-dir", help="schema tree root (default: this checkout's schemas/)")
    p.add_argument("--state-dir", help="state directory (default: ~/.local/state/target-watch)")
    p.add_argument("--nats-url", help="NATS URL (default: $NATS_URL or nats://127.0.0.1:4222)")
    sub = p.add_subparsers(dest="command", required=True)

    for name, helptext in (("match", "inbound messages in, watch.target.detected out"),
                           ("sink", "detections in: ticket, notification, watch.target.routed")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--dry-run", action="store_true", help="print intended actions; publish, file and send nothing")
        s.add_argument("--from-file", help="handle envelopes from a JSON/JSONL file ('-' = stdin) instead of the bus")
        s.add_argument("--since", help="ISO time to start a NEW consumer from (default: new messages only)")
        s.add_argument("--once", action="store_true", help="stop when the consumer has nothing more to deliver")

    g = sub.add_parser("gmail-poll", help="one pass of target-scoped Gmail ingress (Candybar runs it)")
    g.add_argument("--dry-run", action="store_true", help="print the envelopes; publish nothing, mark nothing seen")
    g.add_argument("--account", help="Gmail account gog reads (default: $TARGET_WATCH_GMAIL_ACCOUNT or the operator's)")

    r = sub.add_parser("replay", help="what match would detect in Candystore since a time (publishes nothing)")
    r.add_argument("--since", required=True, help="ISO time, e.g. 2026-11-01T00:00:00Z")
    r.add_argument("--target", help="only this watch_id")
    r.add_argument("--candystore", help="Candystore base URL (default: $CANDYSTORE_URL or http://127.0.0.1:8683)")
    r.add_argument("--json", action="store_true", help="one JSON row per detection")

    sub.add_parser("check", help="validate target files and the x-sender index")

    t = sub.add_parser("status", help="what the sink did for the last detections, channel by channel")
    t.add_argument("--limit", type=_positive, default=10, help="how many detections (default 10)")
    t.add_argument("--target", help="only this watch_id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "replay":
        return cmd_replay(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "status":
        return cmd_status(args)
    runner = {"match": cmd_match, "sink": cmd_sink, "gmail-poll": cmd_gmail_poll}[args.command]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    sys.exit(main())
