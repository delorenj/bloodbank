"""Unit tests for the toaster's dispositions, rate limit, and ntfy pushback.

    uv run --with nats-py --with httpx python -m unittest -v test_main
"""

from __future__ import annotations

import asyncio
import unittest

import main


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeNtfy:
    """Records posts; answers from a script of (status, retry_after) replies."""

    def __init__(self, replies: list[tuple[int, str | None]] | None = None) -> None:
        self.replies = list(replies or [])
        self.posts: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(self, title: str, body: str, headers: dict[str, str]):
        self.posts.append((title, body, headers))
        return self.replies.pop(0) if self.replies else (200, None)


def env(event_type: str, **data) -> dict:
    return {"type": event_type, "source": "urn:test", "data": data}


def make(replies=None, rate_per_min=60.0, burst=2, policy=None, per_type_per_min=0.0, per_type_burst=3):
    clock = Clock()
    ntfy = FakeNtfy(replies)
    toaster = main.Toaster(
        policy=policy or main.Policy(mute=(main.DEFAULT_MUTE.split(",")[0], main.DEFAULT_MUTE.split(",")[1]),
                                     digest=(main.DEFAULT_DIGEST,)),
        post=ntfy,
        bucket=main.TokenBucket(rate_per_min / 60.0, burst, clock=clock),
        clock=clock,
        per_type_rate_per_min=per_type_per_min,
        per_type_burst=per_type_burst,
    )
    return toaster, ntfy, clock


def run(coro):
    return asyncio.run(coro)


class PolicyTest(unittest.TestCase):
    def test_defaults_mute_hook_pulses_and_digest_tool_calls(self):
        policy = main.Policy.from_env({})
        self.assertEqual(policy.classify("bloodbank.agent.hook.updated"), "mute")
        self.assertEqual(policy.classify("bloodbank.system.hook.updated"), "mute")
        self.assertEqual(policy.classify("bloodbank.agent.tool.requested"), "digest")
        self.assertEqual(policy.classify("bloodbank.agent.tool.completed"), "digest")
        self.assertEqual(policy.classify("bloodbank.agent.invocation.skipped"), "toast")
        self.assertEqual(policy.classify("bloodbank.agent.session.started"), "toast")

    def test_empty_env_value_turns_a_list_off(self):
        policy = main.Policy.from_env({"TOASTER_MUTE_TYPES": "", "TOASTER_DIGEST_TYPES": ""})
        self.assertEqual(policy.classify("bloodbank.agent.hook.updated"), "toast")


class DispositionTest(unittest.TestCase):
    def test_muted_and_digested_events_never_post(self):
        toaster, ntfy, _ = make()
        for _ in range(500):
            run(toaster.handle(env("bloodbank.agent.hook.updated"), "s"))
        run(toaster.handle(env("bloodbank.agent.tool.completed", tool_name="Bash"), "s"))
        self.assertEqual(ntfy.posts, [])
        self.assertEqual(toaster.stats["muted"], 500)
        self.assertEqual(toaster.stats["digested"], 1)

    def test_toasts_go_out_individually_until_the_bucket_runs_dry(self):
        toaster, ntfy, clock = make(rate_per_min=60, burst=2)
        results = [run(toaster.handle(env("bloodbank.agent.session.started"), "s")) for _ in range(3)]
        self.assertEqual(results, ["toasted", "toasted", "rate-limited"])
        self.assertEqual(len(ntfy.posts), 2)
        self.assertEqual(ntfy.posts[0][2]["Title"], "bloodbank.agent.session.started")
        clock.now += 1.0  # one token back at 60/min
        self.assertEqual(run(toaster.handle(env("bloodbank.agent.session.started"), "s")), "toasted")

    def test_a_429_pauses_posting_and_counts_instead_of_retrying(self):
        toaster, ntfy, clock = make(replies=[(429, None)], burst=10)
        self.assertEqual(run(toaster.handle(env("bloodbank.agent.session.started"), "s")), "failed")
        self.assertTrue(toaster.paused())
        for _ in range(5):
            self.assertEqual(run(toaster.handle(env("bloodbank.agent.session.ended"), "s")), "rate-limited")
        self.assertEqual(len(ntfy.posts), 1)  # nothing hammered during the pause
        self.assertEqual(toaster.overflow["bloodbank.agent.session.ended"], 5)
        clock.now += toaster.backoff_min + 0.1
        self.assertEqual(run(toaster.handle(env("bloodbank.agent.session.ended"), "s")), "toasted")
        self.assertEqual(toaster.backoff, 0.0)

    def test_backoff_doubles_and_honours_retry_after(self):
        toaster, _, clock = make(replies=[(429, None), (429, None), (429, "42")], burst=10)
        run(toaster.handle(env("x.a"), "s"))
        self.assertAlmostEqual(toaster.paused_until - clock.now, 10.0)
        clock.now = toaster.paused_until
        run(toaster.handle(env("x.a"), "s"))
        self.assertAlmostEqual(toaster.paused_until - clock.now, 20.0)
        clock.now = toaster.paused_until
        run(toaster.handle(env("x.a"), "s"))
        self.assertAlmostEqual(toaster.paused_until - clock.now, 42.0)

    def test_a_4xx_other_than_429_does_not_pause(self):
        toaster, _, _ = make(replies=[(403, None)], burst=10)
        run(toaster.handle(env("x.a"), "s"))
        self.assertFalse(toaster.paused())
        self.assertEqual(toaster.stats["http_403"], 1)


class PerTypeCapTest(unittest.TestCase):
    def test_one_chatty_type_cannot_spend_the_shared_budget(self):
        toaster, ntfy, clock = make(rate_per_min=600.0, burst=20, per_type_per_min=6.0, per_type_burst=3)
        noisy = [run(toaster.handle(env("bloodbank.agent.invocation.started"), "s")) for _ in range(10)]
        self.assertEqual(noisy.count("toasted"), 3)
        self.assertEqual(noisy.count("rate-limited"), 7)
        self.assertEqual(toaster.overflow["bloodbank.agent.invocation.started"], 7)
        # Another type still has its own burst and the shared bucket to spend.
        self.assertEqual(run(toaster.handle(env("bloodbank.agent.session.ended"), "s")), "toasted")
        # The noisy type refills at its own rate: one more token after 10s.
        clock.now += 10
        self.assertEqual(run(toaster.handle(env("bloodbank.agent.invocation.started"), "s")), "toasted")
        self.assertEqual(run(toaster.handle(env("bloodbank.agent.invocation.started"), "s")), "rate-limited")
        self.assertEqual(len(ntfy.posts), 5)

    def test_tracked_types_are_bounded(self):
        toaster, _, _ = make(rate_per_min=6000.0, burst=1000, per_type_per_min=6.0)
        toaster.max_tracked_types = 4
        for i in range(10):
            run(toaster.handle(env(f"bloodbank.test.t{i}.happened"), "s"))
        self.assertEqual(len(toaster.type_buckets), 4)

    def test_zero_turns_the_per_type_cap_off(self):
        toaster, ntfy, _ = make(rate_per_min=6000.0, burst=100, per_type_per_min=0.0)
        for _ in range(10):
            run(toaster.handle(env("bloodbank.agent.invocation.started"), "s"))
        self.assertEqual(len(ntfy.posts), 10)
        self.assertEqual(toaster.type_buckets, {})


class DigestTest(unittest.TestCase):
    def test_digest_rolls_counts_into_one_low_priority_toast(self):
        toaster, ntfy, _ = make(burst=1)
        for _ in range(7):
            run(toaster.handle(env("bloodbank.agent.tool.requested"), "s"))
        for _ in range(3):
            run(toaster.handle(env("bloodbank.agent.session.started"), "s"))  # 1 toasted, 2 overflow
        run(toaster.handle(env("bloodbank.agent.hook.updated"), "s"))
        self.assertTrue(run(toaster.flush_digest()))
        title, body, headers = ntfy.posts[-1]
        self.assertEqual(title, "bloodbank digest: 9 events in 5m")
        self.assertEqual(headers["Priority"], "2")
        self.assertIn("agent.tool.requested x7", body)
        self.assertIn("agent.session.started x2", body)
        self.assertIn("muted: agent.hook.updated x1", body)
        self.assertIsNone(toaster.digest_text())

    def test_muted_counts_alone_never_toast(self):
        toaster, ntfy, _ = make()
        run(toaster.handle(env("bloodbank.agent.hook.updated"), "s"))
        self.assertFalse(run(toaster.flush_digest()))
        self.assertEqual(ntfy.posts, [])

    def test_a_failed_digest_keeps_its_counts(self):
        toaster, _, clock = make(replies=[(429, None)])
        run(toaster.handle(env("bloodbank.agent.tool.requested"), "s"))
        self.assertFalse(run(toaster.flush_digest()))
        self.assertEqual(toaster.digested["bloodbank.agent.tool.requested"], 1)
        self.assertFalse(run(toaster.flush_digest()))  # still paused: no post
        clock.now = toaster.paused_until
        self.assertTrue(run(toaster.flush_digest()))


class RetryAfterTest(unittest.TestCase):
    def test_parses_seconds_and_dates(self):
        self.assertEqual(main.retry_after_seconds("30"), 30.0)
        self.assertIsNone(main.retry_after_seconds(None))
        self.assertIsNone(main.retry_after_seconds("soon"))
        self.assertAlmostEqual(
            main.retry_after_seconds("Thu, 01 Jan 1970 00:01:40 GMT", now=40.0), 60.0
        )


if __name__ == "__main__":
    unittest.main()
