#!/usr/bin/env python3
"""Publish ONE synthetic event per bloodbank.genesis.* type, to prove the pipeline end to end.

Every event is built with the canonical builder, validated against its schema, and published to
JetStream with Nats-Msg-Id = envelope id. They are marked data.test=true, so ntfy shows "[test]".
This buzzes the operator's phone five times; run it once, not in a loop.

  python3 smoke.py            publish the set
  python3 smoke.py --dry-run  build and validate only
"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[3] / "services" / "agent-hooks"
sys.path.insert(0, str(HOOKS))
from core.envelope import build_envelope  # noqa: E402
from core.validate import validate_envelope  # noqa: E402

CLIENT = {
    "schema_version": 1, "client_id": "test", "client_name": "Genesis test", "bot_id": "test:assistant",
    "operator_url": "https://genesis.delo.sh/operator?client=test", "test": True,
}


def corr(kind: str, ident: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:33god:genesis:{kind}:{ident}"))


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build(ctype: str, kind: str, ident: str, data: dict) -> dict:
    c = corr(kind, ident)
    env = build_envelope(
        ce_type=ctype, source="urn:33god:service:genesis", producer="genesis", service="genesis",
        actor={"type": "agent", "agent_id": "test:assistant"},
        data={**CLIENT, "occurred_at": now(), **data}, correlation_id=c, causation_id=c,
        ordering_key=f"{kind}:{ident}",
    )
    validate_envelope(env)
    return env


def events() -> list[dict]:
    run = uuid.uuid4().hex[:8]
    aid, lid = f"prop-test-{run}", f"alert-test-{run}"
    ap = {"approval_id": aid, "kind": "email.send"}
    out = [build("bloodbank.genesis.approval.requested", "approval", aid, {
        **ap, "summary": "Send the weekly recap email (synthetic)",
        "question": "Approve sending the weekly recap? Synthetic event from the Genesis -> ntfy smoke test.",
        "channel": "web", "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400)),
        "action": {"to": ["person@example.com"], "subject": "Weekly recap",
                   "body_preview": "Hi all, here is this week's recap."}})]
    for action, via in (("granted", "web"), ("denied", "telegram"), ("expired", "expiry")):
        out.append(build(f"bloodbank.genesis.approval.{action}", "approval", aid, {
            **ap, "summary": f"Send the weekly recap email (synthetic): {action}", "status": action, "decided_via": via}))
    out.append(build("bloodbank.genesis.alert.raised", "alert", lid, {
        "alert_id": lid, "reason": "operator_requested", "severity": "warning",
        "summary": "A desk asked for the operator (synthetic)", "detail": "Smoke test alert; nothing is wrong."}))
    return out


async def main(dry: bool) -> None:
    evs = events()
    for e in evs:
        print("built", e["type"], e["id"])
    if dry:
        return
    import nats
    nc = await nats.connect("nats://127.0.0.1:4222")
    js = nc.jetstream()
    for e in evs:
        ack = await js.publish(e["subject"], json.dumps(e).encode(), headers={"Nats-Msg-Id": e["id"]})
        print("published", e["subject"], "stream", ack.stream, "seq", ack.seq)
        await asyncio.sleep(1.5)
    await nc.drain()


asyncio.run(main("--dry-run" in sys.argv))
