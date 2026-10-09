"""Unit tests for the router's dispositions, rate limit, ntfy pushback, and the
link buttons and deployment notifications.

    python3 -m unittest -v test_main
    uv run --with nats-py --with httpx python -m unittest -v test_main

The deployment samples below pass `bb-emit --check` against
schemas/bloodbank/project/deployment.{completed,failed}.json (2026-10-09).
"""

from __future__ import annotations

import asyncio
import copy
import json
import unittest

import main

try:
    import httpx
except ImportError:  # the wire tests need httpx; the rest do not
    httpx = None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeNtfy:
    """Records posts; answers from a script of (status, retry_after) replies.

    posts keeps (title, body, headers) per call; event_types and messages
    (the JSON publish, None for a header publish) run alongside it."""

    def __init__(self, replies: list[tuple[int, str | None]] | None = None) -> None:
        self.replies = list(replies or [])
        self.posts: list[tuple[str, str, dict[str, str]]] = []
        self.event_types: list[str] = []
        self.messages: list[dict | None] = []

    async def __call__(self, event_type: str, title: str, body: str, headers: dict[str, str], message=None):
        self.event_types.append(event_type)
        self.posts.append((title, body, headers))
        self.messages.append(message)
        return self.replies.pop(0) if self.replies else (200, None)


class FakeSizer:
    def __init__(self, size: int | None = None, error: Exception | None = None) -> None:
        self.size, self.error, self.urls = size, error, []

    async def __call__(self, url: str):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.size


def env(event_type: str, **data) -> dict:
    return {"type": event_type, "source": "urn:test", "data": data}


def make(replies=None, rate_per_min=60.0, burst=2, policy=None, per_type_per_min=0.0, per_type_burst=3, sizer=None):
    clock = Clock()
    ntfy = FakeNtfy(replies)
    router = main.NtfyRouter(
        policy=policy or main.Policy(mute=(main.DEFAULT_MUTE.split(",")[0], main.DEFAULT_MUTE.split(",")[1]),
                                     digest=(main.DEFAULT_DIGEST,)),
        post=ntfy,
        bucket=main.TokenBucket(rate_per_min / 60.0, burst, clock=clock),
        clock=clock,
        per_type_rate_per_min=per_type_per_min,
        per_type_burst=per_type_burst,
        sizer=sizer,
    )
    return router, ntfy, clock


def run(coro):
    return asyncio.run(coro)


# -- deployment samples (mobile-deploy-hub v1.1) ---------------------------------

RUN_URL = "https://github.com/delorenj/pile-of-dumb-things/actions/runs/37940000000"
APK_URL = "https://s3.delo.sh/builds/tower-of-dumb-things/0.2.2/tower-of-dumb-things-0.2.2-4.apk"
OTA_URL = "itms-services://?action=download-manifest&url=https://s3.delo.sh/builds/tower-of-dumb-things/ios/manifest.plist"
PAGE_URL = "https://s3.delo.sh/builds/tower-of-dumb-things/index.html"
APK = {"rel": "apk", "label": "Install APK", "url": APK_URL}
OTA = {"rel": "ota", "label": "Install on iPad", "url": OTA_URL}
PAGE = {"rel": "page", "label": "Builds", "url": PAGE_URL}
RUN = {"rel": "run", "label": "Run", "url": RUN_URL}
SHA = "ab" * 32

S26_PENDING = {"platform": "android", "target": "SM-S948U", "device": "S26", "version": "0.2.2", "build": "4",
               "sha256": None, "verified": False, "status": "pending", "by": "deploy",
               "reason": "not on big-chungus's adb"}
IPAD_PENDING = {"platform": "ios", "target": "iPad14,5", "device": "iPad", "version": "0.2.2", "build": "4",
                "sha256": None, "verified": False, "status": "pending", "by": "deploy",
                "reason": "unavailable in devicectl"}
IPAD_INSTALLED = {"platform": "ios", "target": "iPad14,5", "device": "iPad", "version": "0.2.2", "build": "4",
                  "sha256": SHA, "verified": True, "status": "installed", "by": "deploy"}


def deployment(event_type=main.DEPLOYMENT_COMPLETED, deliveries=(S26_PENDING, IPAD_INSTALLED),
               links=(APK, PAGE, RUN), trigger="deploy", target=None, name="Tower of Lost Things", **extra) -> dict:
    data = {
        "schema_version": 1,
        "summary": "Tower of Lost Things 0.2.2 (4) 1a2b3c4: S26 pending, iPad installed, verified=false",
        "project": {"slug": "tower-of-dumb-things", "name": name},
        "artifact": {"kind": "mobile-app", "name": name, "version": "0.2.2", "version_code": 4,
                     "commit": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b", "dirty": False, "variant": "debug",
                     "sha256": None},
        "target": target or {"kind": "device-set", "name": "SM-S948U + iPad14,5", "id": None},
        "deliveries": [dict(d) for d in deliveries],
        "run": {"id": "37940000000", "attempt": 1, "url": RUN_URL, "hub_ref": "v1.1.0"},
        "trigger": trigger,
    }
    if links is not None:
        data["links"] = [dict(link) for link in links]
    if event_type == main.DEPLOYMENT_COMPLETED:
        data.update(verified=all(d.get("verified") for d in deliveries), deployed_at="2026-10-09T20:00:00Z")
    else:
        data.update(failed_at="2026-10-09T20:00:00Z")
    data.update(extra)
    return {"type": event_type, "source": "urn:33god:service:mobile-deploy-hub", "data": data}


def catch_up_s26() -> dict:
    return deployment(
        deliveries=[{"platform": "android", "target": "SM-S948U", "device": "S26", "version": "0.2.2", "build": "4",
                     "sha256": SHA, "verified": True, "status": "installed", "by": "catch-up"}],
        links=[PAGE], trigger="catch-up", target={"kind": "android-device", "name": "SM-S948U", "id": "R5GL43A7PCH"},
    )


def catch_up_ipad() -> dict:
    return deployment(
        deliveries=[dict(IPAD_INSTALLED, by="catch-up")], links=[PAGE], trigger="catch-up",
        target={"kind": "ios-device", "name": "iPad14,5", "id": "00008112-000138390CFB401E"},
    )


def message_of(envelope: dict, apk_bytes=None) -> dict:
    links = main.usable_links(envelope["data"], envelope["type"])
    return main.render_links(envelope, "s", links, priority="5", tags="drop_of_blood,zap", apk_bytes=apk_bytes)


# -- the dispositions, buckets and pushback (unchanged behaviour) ------------------


class PolicyTest(unittest.TestCase):
    def test_defaults_mute_hook_pulses_and_digest_tool_calls(self):
        policy = main.Policy.from_env({})
        self.assertEqual(policy.classify("bloodbank.agent.hook.updated"), "mute")
        self.assertEqual(policy.classify("bloodbank.system.hook.updated"), "mute")
        self.assertEqual(policy.classify("bloodbank.agent.tool.requested"), "digest")
        self.assertEqual(policy.classify("bloodbank.agent.tool.completed"), "digest")
        self.assertEqual(policy.classify("bloodbank.agent.invocation.skipped"), "route")
        self.assertEqual(policy.classify("bloodbank.agent.session.started"), "route")

    def test_empty_env_value_turns_a_list_off(self):
        policy = main.Policy.from_env({"NTFY_ROUTER_MUTE_TYPES": "", "NTFY_ROUTER_DIGEST_TYPES": ""})
        self.assertEqual(policy.classify("bloodbank.agent.hook.updated"), "route")

    def test_deployments_are_loud_by_default_and_the_list_can_be_emptied(self):
        policy = main.Policy.from_env({})
        self.assertTrue(policy.is_loud("bloodbank.project.deployment.completed"))
        self.assertTrue(policy.is_loud("bloodbank.project.deployment.failed"))
        self.assertFalse(policy.is_loud("bloodbank.llm.usage.recorded"))
        self.assertEqual(policy.classify("bloodbank.project.deployment.completed"), "route")
        self.assertFalse(main.Policy.from_env({"NTFY_ROUTER_LOUD_TYPES": ""}).is_loud(main.DEPLOYMENT_COMPLETED))


class DispositionTest(unittest.TestCase):
    def test_muted_and_digested_events_never_post(self):
        router, ntfy, _ = make()
        for _ in range(500):
            run(router.handle(env("bloodbank.agent.hook.updated"), "s"))
        run(router.handle(env("bloodbank.agent.tool.completed", tool_name="Bash"), "s"))
        self.assertEqual(ntfy.posts, [])
        self.assertEqual(router.stats["muted"], 500)
        self.assertEqual(router.stats["digested"], 1)

    def test_routes_go_out_individually_until_the_bucket_runs_dry(self):
        router, ntfy, clock = make(rate_per_min=60, burst=2)
        results = [run(router.handle(env("bloodbank.agent.session.started"), "s")) for _ in range(3)]
        self.assertEqual(results, ["routed", "routed", "rate-limited"])
        self.assertEqual(len(ntfy.posts), 2)
        self.assertEqual(ntfy.posts[0][2]["Title"], "bloodbank.agent.session.started")
        clock.now += 1.0  # one token back at 60/min
        self.assertEqual(run(router.handle(env("bloodbank.agent.session.started"), "s")), "routed")

    def test_a_429_pauses_posting_and_counts_instead_of_retrying(self):
        router, ntfy, clock = make(replies=[(429, None)], burst=10)
        self.assertEqual(run(router.handle(env("bloodbank.agent.session.started"), "s")), "failed")
        self.assertTrue(router.paused())
        for _ in range(5):
            self.assertEqual(run(router.handle(env("bloodbank.agent.session.ended"), "s")), "rate-limited")
        self.assertEqual(len(ntfy.posts), 1)  # nothing hammered during the pause
        self.assertEqual(router.overflow["bloodbank.agent.session.ended"], 5)
        clock.now += router.backoff_min + 0.1
        self.assertEqual(run(router.handle(env("bloodbank.agent.session.ended"), "s")), "routed")
        self.assertEqual(router.backoff, 0.0)

    def test_backoff_doubles_and_honours_retry_after(self):
        router, _, clock = make(replies=[(429, None), (429, None), (429, "42")], burst=10)
        run(router.handle(env("x.a"), "s"))
        self.assertAlmostEqual(router.paused_until - clock.now, 10.0)
        clock.now = router.paused_until
        run(router.handle(env("x.a"), "s"))
        self.assertAlmostEqual(router.paused_until - clock.now, 20.0)
        clock.now = router.paused_until
        run(router.handle(env("x.a"), "s"))
        self.assertAlmostEqual(router.paused_until - clock.now, 42.0)

    def test_a_4xx_other_than_429_does_not_pause(self):
        router, _, _ = make(replies=[(403, None)], burst=10)
        run(router.handle(env("x.a"), "s"))
        self.assertFalse(router.paused())
        self.assertEqual(router.stats["http_403"], 1)


class PerTypeCapTest(unittest.TestCase):
    def test_one_chatty_type_cannot_spend_the_shared_budget(self):
        router, ntfy, clock = make(rate_per_min=600.0, burst=20, per_type_per_min=6.0, per_type_burst=3)
        noisy = [run(router.handle(env("bloodbank.agent.invocation.started"), "s")) for _ in range(10)]
        self.assertEqual(noisy.count("routed"), 3)
        self.assertEqual(noisy.count("rate-limited"), 7)
        self.assertEqual(router.overflow["bloodbank.agent.invocation.started"], 7)
        # Another type still has its own burst and the shared bucket to spend.
        self.assertEqual(run(router.handle(env("bloodbank.agent.session.ended"), "s")), "routed")
        # The noisy type refills at its own rate: one more token after 10s.
        clock.now += 10
        self.assertEqual(run(router.handle(env("bloodbank.agent.invocation.started"), "s")), "routed")
        self.assertEqual(run(router.handle(env("bloodbank.agent.invocation.started"), "s")), "rate-limited")
        self.assertEqual(len(ntfy.posts), 5)

    def test_tracked_types_are_bounded(self):
        router, _, _ = make(rate_per_min=6000.0, burst=1000, per_type_per_min=6.0)
        router.max_tracked_types = 4
        for i in range(10):
            run(router.handle(env(f"bloodbank.test.t{i}.happened"), "s"))
        self.assertEqual(len(router.type_buckets), 4)

    def test_zero_turns_the_per_type_cap_off(self):
        router, ntfy, _ = make(rate_per_min=6000.0, burst=100, per_type_per_min=0.0)
        for _ in range(10):
            run(router.handle(env("bloodbank.agent.invocation.started"), "s"))
        self.assertEqual(len(ntfy.posts), 10)
        self.assertEqual(router.type_buckets, {})


class DigestTest(unittest.TestCase):
    def test_digest_rolls_counts_into_one_low_priority_route(self):
        router, ntfy, _ = make(burst=1)
        for _ in range(7):
            run(router.handle(env("bloodbank.agent.tool.requested"), "s"))
        for _ in range(3):
            run(router.handle(env("bloodbank.agent.session.started"), "s"))  # 1 routed, 2 overflow
        run(router.handle(env("bloodbank.agent.hook.updated"), "s"))
        self.assertTrue(run(router.flush_digest()))
        title, body, headers = ntfy.posts[-1]
        self.assertEqual(title, "bloodbank digest: 9 events in 5m")
        self.assertEqual(headers, {"Title": title, "Priority": "2", "Tags": "drop_of_blood,bar_chart"})
        self.assertIsNone(ntfy.messages[-1])
        self.assertIn("agent.tool.requested x7", body)
        self.assertIn("agent.session.started x2", body)
        self.assertIn("muted: agent.hook.updated x1", body)
        self.assertIsNone(router.digest_text())

    def test_muted_counts_alone_never_route(self):
        router, ntfy, _ = make()
        run(router.handle(env("bloodbank.agent.hook.updated"), "s"))
        self.assertFalse(run(router.flush_digest()))
        self.assertEqual(ntfy.posts, [])

    def test_a_failed_digest_keeps_its_counts(self):
        router, _, clock = make(replies=[(429, None)])
        run(router.handle(env("bloodbank.agent.tool.requested"), "s"))
        self.assertFalse(run(router.flush_digest()))
        self.assertEqual(router.digested["bloodbank.agent.tool.requested"], 1)
        self.assertFalse(run(router.flush_digest()))  # still paused: no post
        clock.now = router.paused_until
        self.assertTrue(run(router.flush_digest()))


class RetryAfterTest(unittest.TestCase):
    def test_parses_seconds_and_dates(self):
        self.assertEqual(main.retry_after_seconds("30"), 30.0)
        self.assertIsNone(main.retry_after_seconds(None))
        self.assertIsNone(main.retry_after_seconds("soon"))
        self.assertAlmostEqual(
            main.retry_after_seconds("Thu, 01 Jan 1970 00:01:40 GMT", now=40.0), 60.0
        )


# -- links -> ntfy view actions and Click ----------------------------------------


class LinksTest(unittest.TestCase):
    def test_each_link_becomes_a_view_action_that_clears_the_notification(self):
        actions = main.link_actions(main.usable_links({"links": [APK, PAGE, RUN]}))
        self.assertEqual(actions, [
            {"action": "view", "label": "Install APK", "url": APK_URL, "clear": True},
            {"action": "view", "label": "Builds", "url": PAGE_URL, "clear": True},
            {"action": "view", "label": "Run", "url": RUN_URL, "clear": True},
        ])

    def test_at_most_three_and_run_gives_way_to_three_better_links(self):
        for order in ([APK, OTA, PAGE, RUN], [RUN, APK, OTA, PAGE]):
            labels = [a["label"] for a in main.link_actions(main.usable_links({"links": order}))]
            self.assertEqual(labels, ["Install APK", "Install on iPad", "Builds"])
        five = [{"rel": "release", "label": f"L{i}", "url": f"https://x.test/{i}"} for i in range(5)]
        self.assertEqual([a["label"] for a in main.link_actions(main.usable_links({"links": five}))], ["L0", "L1", "L2"])

    def test_a_run_link_keeps_its_place_when_a_slot_is_free(self):
        links = main.usable_links({"links": [dict(RUN, label="Open run"), PAGE]})
        self.assertEqual([a["label"] for a in main.link_actions(links)], ["Open run", "Builds"])
        links = main.usable_links({"links": [RUN, APK, PAGE]})
        self.assertEqual([a["label"] for a in main.link_actions(links)], ["Run", "Install APK", "Builds"])

    def test_only_https_and_itms_services_urls_pass_and_drops_are_logged(self):
        bad = [
            {"rel": "page", "label": "plain http", "url": "http://s3.delo.sh/builds/index.html"},
            {"rel": "page", "label": "script", "url": "javascript:alert(1)"},
            {"rel": "page", "label": "upper", "url": "HTTPS://s3.delo.sh/x"},
            {"rel": "page", "label": "space", "url": "https://s3.delo.sh/a b"},
            {"rel": "page", "label": "newline", "url": "https://s3.delo.sh/x\n"},
            {"rel": "page", "label": "quote", "url": 'https://s3.delo.sh/x"y'},
            {"rel": "page", "label": "long", "url": "https://s3.delo.sh/" + "a" * 600},
            {"rel": "page", "label": "", "url": PAGE_URL},
            {"rel": "page", "url": PAGE_URL},
            "not an object",
        ]
        with self.assertLogs("ntfy-router", level="WARNING") as logs:
            links = main.usable_links({"links": bad + [OTA]}, "bloodbank.project.deployment.completed")
        self.assertEqual(links, [OTA])
        self.assertEqual(len(logs.records), len(bad))
        self.assertIn("dropped link on bloodbank.project.deployment.completed", logs.output[0])

    def test_labels_are_trimmed_to_32_characters(self):
        links = main.usable_links({"links": [{"rel": "page", "label": "  Builds  for   every\tapp " + "x" * 40,
                                              "url": PAGE_URL}]})
        self.assertEqual(links[0]["label"], "Builds for every app xxxxxxxxxxx")
        self.assertLessEqual(len(links[0]["label"]), 32)

    def test_click_is_the_page_then_the_run_then_the_first_https_link(self):
        click = lambda links: main.link_click(main.usable_links({"links": links}))  # noqa: E731
        self.assertEqual(click([APK, OTA, RUN, PAGE]), PAGE_URL)
        self.assertEqual(click([APK, OTA, RUN]), RUN_URL)
        self.assertEqual(click([OTA, APK]), APK_URL)
        self.assertIsNone(click([OTA]))  # never an itms-services click

    def test_commas_semicolons_and_quotes_need_no_escaping_in_the_json_publish(self):
        tricky = {"rel": "page", "label": 'Builds, "all"; apps', "url": "https://s3.delo.sh/b,c;d/index.html?x=1,2;y"}
        message = message_of(deployment(links=[APK, tricky]))
        self.assertEqual(message["actions"][1], {"action": "view", "label": 'Builds, "all"; apps',
                                                 "url": "https://s3.delo.sh/b,c;d/index.html?x=1,2;y", "clear": True})
        # The wire form is plain JSON: ntfy marshals `actions` into X-Actions as a
        # JSON array (parseActionsFromJSON), so the simple format's quoting
        # rules never apply.
        _, _, content = main.ntfy_request(main.DEPLOYMENT_COMPLETED, message["message"], {}, message)
        self.assertEqual(json.loads(content)["actions"], message["actions"])

    def test_events_without_a_list_of_links_have_none(self):
        self.assertEqual(main.usable_links({"links": "https://x.test"}), [])
        self.assertEqual(main.usable_links({}), [])
        self.assertEqual(main.usable_links("not a dict"), [])


# -- the publish: JSON to the ntfy root for links, headers otherwise ----------------


class PublishTest(unittest.TestCase):
    def test_a_link_event_is_one_json_publish_with_the_ntfy_fields_and_no_attach(self):
        router, ntfy, _ = make(burst=10)
        self.assertEqual(run(router.handle(deployment(), "s")), "routed")
        title, body, headers = ntfy.posts[0]
        message = ntfy.messages[0]
        self.assertEqual(headers, {})
        self.assertEqual(list(message), ["topic", "title", "message", "priority", "tags", "click", "actions"])
        self.assertEqual(message["topic"], main.NTFY_TOPIC)
        self.assertEqual((title, body), (message["title"], message["message"]))
        self.assertEqual(message["click"], PAGE_URL)
        self.assertNotIn("attach", message)
        self.assertNotIn("filename", message)

    def test_any_event_type_with_links_gets_buttons_with_its_usual_title_and_priority(self):
        router, ntfy, _ = make(burst=10)
        event = env("bloodbank.project.release.published", summary="v2 is out", links=[PAGE])
        self.assertEqual(run(router.handle(event, "s")), "routed")
        message = ntfy.messages[0]
        self.assertEqual(message["title"], "bloodbank.project.release.published")
        self.assertEqual(message["message"], "src: urn:test\nsummary: v2 is out")
        self.assertEqual((message["priority"], message["tags"]), (5, ["drop_of_blood", "zap"]))
        self.assertEqual(message["actions"], [{"action": "view", "label": "Builds", "url": PAGE_URL, "clear": True}])

    def test_slop_events_keep_their_topic_with_links_too(self):
        self.assertEqual(message_of(env("bloodbank.review.slop.found", links=[PAGE]))["topic"], "slop")
        self.assertEqual(main.ntfy_request("bloodbank.review.slop.found", "b", {})[0], f"{main.NTFY_URL}/slop")

    def test_utf8_titles_survive_the_json_publish(self):
        message = message_of(deployment(name="Grüvato ✨"))
        self.assertEqual(message["title"], "Grüvato ✨ 0.2.2 deployed")
        _, headers, content = main.ntfy_request(main.DEPLOYMENT_COMPLETED, message["message"], {}, message)
        self.assertEqual(headers, {"Content-Type": "application/json"})
        self.assertIn("Grüvato ✨ 0.2.2 deployed".encode("utf-8"), content)

    def test_events_without_links_keep_the_header_publish(self):
        router, ntfy, _ = make(burst=10)
        run(router.handle(env("bloodbank.agent.session.started", summary="hello"), "s"))
        self.assertEqual(ntfy.posts[0], ("bloodbank.agent.session.started", "src: urn:test\nsummary: hello",
                                         {"Title": "bloodbank.agent.session.started", "Priority": "5",
                                          "Tags": "drop_of_blood,zap"}))
        self.assertIsNone(ntfy.messages[0])

    def test_a_deployment_without_links_is_rendered_as_it_always_was(self):
        router, ntfy, _ = make(burst=10)
        run(router.handle(deployment(links=None), "s"))
        title, body, headers = ntfy.posts[0]
        self.assertEqual(title, main.DEPLOYMENT_COMPLETED)
        self.assertEqual(body, "src: urn:33god:service:mobile-deploy-hub\nsummary: "
                               "Tower of Lost Things 0.2.2 (4) 1a2b3c4: S26 pending, iPad installed, verified=false")
        self.assertIsNone(ntfy.messages[0])

    def test_links_that_all_drop_fall_back_to_the_header_publish(self):
        router, ntfy, _ = make(burst=10)
        with self.assertLogs("ntfy-router", level="WARNING"):
            run(router.handle(deployment(links=[{"rel": "apk", "label": "x", "url": "http://insecure"}]), "s"))
        self.assertIsNone(ntfy.messages[0])
        self.assertEqual(ntfy.posts[0][2]["Title"], main.DEPLOYMENT_COMPLETED)

    def test_header_titles_are_ascii_only_and_ascii_titles_pass_unchanged(self):
        self.assertEqual(main.header_text("bloodbank.agent.session.started"), "bloodbank.agent.session.started")
        self.assertEqual(main.header_text("bloodbank digest: 9 events in 5m"), "bloodbank digest: 9 events in 5m")
        folded = main.header_text("bloodbank.test.grüvato✨\nnext")
        self.assertTrue(folded.isascii() and folded.isprintable())
        self.assertEqual(folded, "bloodbank.test.gruvato next")
        router, ntfy, _ = make(burst=10)
        run(router.handle(env("bloodbank.test.grüße"), "s"))
        self.assertTrue(ntfy.posts[0][2]["Title"].isascii())

    def test_priority_names_and_numbers_both_become_ntfy_numbers(self):
        self.assertEqual([main.priority_number(v) for v in ("5", "1", "high", "MAX", "urgent", "min", "9", "?")],
                         [5, 1, 4, 5, 5, 1, 3, 3])


# -- deployment titles and bodies --------------------------------------------------


class DeploymentTest(unittest.TestCase):
    def test_a_deploy_with_both_devices_away_offers_both_installs(self):
        message = message_of(deployment(deliveries=[S26_PENDING, IPAD_PENDING], links=[APK, OTA, PAGE, RUN]),
                             apk_bytes=72_300_000)
        self.assertEqual(message["title"], "Tower of Lost Things 0.2.2 deployed")
        self.assertEqual(message["message"], "\n".join([
            "S26: pending (not on big-chungus's adb)",
            "iPad: pending (unavailable in devicectl)",
            "APK 72 MB",
            f"Run: {RUN_URL}",
        ]))
        self.assertEqual((message["priority"], message["tags"], message["click"]), (5, ["package"], PAGE_URL))
        self.assertEqual([(a["label"], a["url"]) for a in message["actions"]],
                         [("Install APK", APK_URL), ("Install on iPad", OTA_URL), ("Builds", PAGE_URL)])

    def test_the_design_example_body(self):
        message = message_of(deployment(), apk_bytes=72_300_000)
        self.assertEqual(message["message"].splitlines()[:3], [
            "S26: pending (not on big-chungus's adb)",
            "iPad: installed 0.2.2 (4), verified",
            "APK 72 MB",
        ])
        self.assertEqual([a["label"] for a in message["actions"]], ["Install APK", "Builds", "Run"])

    def test_skipped_and_failed_deliveries_say_why(self):
        skipped = dict(IPAD_PENDING, status="skipped", reason="carries-macbook-air offline")
        failed = dict(S26_PENDING, status="failed", stage="install", reason="INSTALL_FAILED_UPDATE_INCOMPATIBLE")
        lines = message_of(deployment(deliveries=[failed, skipped]))["message"].splitlines()
        self.assertEqual(lines[:2], ["S26: failed at install (INSTALL_FAILED_UPDATE_INCOMPATIBLE)",
                                     "iPad: skipped (carries-macbook-air offline)"])

    def test_a_catch_up_install_names_its_device(self):
        message = message_of(catch_up_s26())
        self.assertEqual(message["title"], "Tower of Lost Things 0.2.2 installed on S26")
        self.assertEqual(message["message"], "S26: installed 0.2.2 (4) by catch-up, verified")
        self.assertEqual((message["priority"], message["tags"]), (4, ["white_check_mark"]))
        self.assertEqual(message["actions"], [{"action": "view", "label": "Builds", "url": PAGE_URL, "clear": True}])
        manual = catch_up_s26()
        manual["data"]["deliveries"][0]["by"] = "manual"
        self.assertEqual(message_of(manual)["message"], "S26: installed 0.2.2 (4) by hand, verified")

    def test_a_failed_deploy_names_the_stage_the_reason_and_the_run(self):
        event = deployment(main.DEPLOYMENT_FAILED, deliveries=[dict(S26_PENDING, status="failed", stage="build")],
                           links=[dict(RUN, label="Open run")], stage="build", reason="gradle assembleDebug exited 1")
        message = message_of(event)
        self.assertEqual(message["title"], "Tower of Lost Things 0.2.2 deploy failed at build")
        self.assertEqual(message["message"], f"build: gradle assembleDebug exited 1\nRun: {RUN_URL}")
        self.assertEqual((message["priority"], message["tags"], message["click"]), (5, ["rotating_light"], RUN_URL))
        self.assertEqual([a["label"] for a in message["actions"]], ["Open run"])

    def test_a_failed_publish_links_the_build_page_and_the_run(self):
        event = deployment(main.DEPLOYMENT_FAILED, links=[dict(RUN, label="Open run"), PAGE], stage="deliver",
                           reason="mc cp refused: the builds bucket is full")
        message = message_of(event)
        self.assertEqual(message["title"], "Tower of Lost Things 0.2.2 deploy failed at deliver")
        self.assertEqual(message["click"], PAGE_URL)
        self.assertEqual([a["label"] for a in message["actions"]], ["Open run", "Builds"])

    def test_a_failed_catch_up_names_its_device(self):
        reason = "devicectl install failed 3 times: the iPad is locked"
        event = deployment(main.DEPLOYMENT_FAILED, trigger="catch-up", links=[PAGE, RUN], stage="install",
                           reason=reason, target={"kind": "ios-device", "name": "iPad14,5", "id": "00008112-000138390CFB401E"},
                           deliveries=[dict(IPAD_PENDING, status="failed", stage="install", reason=reason, by="catch-up")])
        message = message_of(event)
        self.assertEqual(message["title"], "Tower of Lost Things 0.2.2 catch-up failed on iPad")
        self.assertEqual(message["message"], f"install: {reason}\nRun: {RUN_URL}")
        self.assertEqual((message["priority"], message["tags"]), (4, ["warning"]))

    def test_a_single_target_deploy_gets_one_line_from_its_target(self):
        event = deployment(target={"kind": "android-device", "name": "SM-S948U", "id": "R5GL43A7PCH"},
                           deliveries=[], links=[PAGE])
        del event["data"]["deliveries"]
        event["data"]["verified"] = True
        self.assertEqual(message_of(event)["message"], "SM-S948U: installed 0.2.2 (4), verified")

    def test_the_size_line_needs_an_apk_link(self):
        self.assertEqual(message_of(deployment(), apk_bytes=312_000_000)["message"].splitlines()[2], "APK 312 MB")
        self.assertNotIn("APK", message_of(deployment(links=[PAGE]), apk_bytes=72_300_000)["message"])
        self.assertEqual(main.megabytes(5_240_000), "5.2 MB")

    def test_the_router_sizes_the_apk_only_for_a_completed_deploy(self):
        sizer = FakeSizer(size=96_000_000)
        router, ntfy, _ = make(burst=10, sizer=sizer)
        run(router.handle(deployment(), "s"))
        self.assertEqual(sizer.urls, [APK_URL])
        self.assertIn("APK 96 MB", ntfy.messages[0]["message"])
        run(router.handle(catch_up_ipad(), "s"))
        run(router.handle(deployment(main.DEPLOYMENT_FAILED, links=[APK, RUN], stage="install", reason="x"), "s"))
        self.assertEqual(sizer.urls, [APK_URL])

    def test_a_failed_size_lookup_still_routes_without_the_line(self):
        router, ntfy, _ = make(burst=10, sizer=FakeSizer(error=RuntimeError("boom")))
        with self.assertLogs("ntfy-router", level="WARNING"):
            self.assertEqual(run(router.handle(deployment(), "s")), "routed")
        self.assertNotIn("APK", ntfy.messages[0]["message"])


# -- loud types and duplicates -----------------------------------------------------


class LoudTest(unittest.TestCase):
    def test_deployments_skip_the_per_type_cap_and_the_shared_bucket(self):
        router, ntfy, _ = make(rate_per_min=1.0, burst=1, per_type_per_min=1.0, per_type_burst=1)
        events = [deployment(), catch_up_s26(), catch_up_ipad(),
                  deployment(main.DEPLOYMENT_FAILED, links=[RUN], stage="build", reason="x")] * 3
        for event in events:
            event = copy.deepcopy(event)
            event["data"]["run"]["id"] = str(len(ntfy.posts))  # no two catch-ups of one run and device
            self.assertEqual(run(router.handle(event, "s")), "routed")
        self.assertEqual(len(ntfy.posts), 12)
        self.assertEqual(router.type_buckets, {})
        self.assertEqual(router.bucket.tokens, 1.0)  # the shared bucket was never touched
        self.assertEqual(run(router.handle(env("bloodbank.llm.usage.recorded"), "s")), "routed")
        self.assertEqual(run(router.handle(env("bloodbank.llm.usage.recorded"), "s")), "rate-limited")

    def test_deployments_still_wait_out_ntfy_pushback_and_join_the_digest(self):
        router, ntfy, clock = make(replies=[(429, "30")], burst=10)
        self.assertEqual(run(router.handle(env("bloodbank.llm.usage.recorded"), "s")), "failed")
        self.assertEqual(run(router.handle(deployment(), "s")), "rate-limited")
        self.assertEqual(len(ntfy.posts), 1)
        self.assertIn("project.deployment.completed x1", router.digest_text()[1])
        clock.now += 31
        self.assertEqual(run(router.handle(deployment(), "s")), "routed")

    def test_an_explicit_mute_still_wins(self):
        policy = main.Policy(mute=("bloodbank.project.*",), digest=())
        router, ntfy, _ = make(policy=policy)
        self.assertEqual(run(router.handle(deployment(), "s")), "muted")
        self.assertEqual(ntfy.posts, [])


class DuplicateTest(unittest.TestCase):
    def test_no_catch_up_notification_for_a_device_the_run_installed(self):
        router, ntfy, _ = make(burst=10)
        self.assertEqual(run(router.handle(deployment(), "s")), "routed")  # iPad installed, S26 pending
        self.assertEqual(run(router.handle(catch_up_ipad(), "s")), "duplicate")
        self.assertEqual(run(router.handle(catch_up_s26(), "s")), "routed")
        self.assertEqual(run(router.handle(catch_up_s26(), "s")), "duplicate")  # a retried copy
        self.assertEqual(len(ntfy.posts), 2)
        self.assertEqual(router.stats["duplicate"], 2)

    def test_reruns_and_other_runs_still_notify(self):
        router, ntfy, _ = make(burst=10)
        run(router.handle(deployment(), "s"))
        run(router.handle(deployment(), "s"))  # a rerun's deploy event is never held back
        other = catch_up_ipad()
        other["data"]["run"]["id"] = "37950000000"
        self.assertEqual(run(router.handle(other, "s")), "routed")
        self.assertEqual(len(ntfy.posts), 3)

    def test_a_catch_up_that_did_not_go_out_is_not_remembered(self):
        router, ntfy, clock = make(replies=[(503, "5")], burst=10)
        self.assertEqual(run(router.handle(catch_up_s26(), "s")), "failed")
        clock.now += 6
        self.assertEqual(run(router.handle(catch_up_s26(), "s")), "routed")

    def test_remembered_installs_are_bounded(self):
        router, _, _ = make(burst=10)
        router.max_remembered_installs = 3
        for i in range(5):
            event = catch_up_s26()
            event["data"]["run"]["id"] = str(i)
            run(router.handle(event, "s"))
        self.assertEqual(len(router.installs), 3)


# -- the real transport, on the wire (httpx.MockTransport) ---------------------------


@unittest.skipIf(httpx is None, "httpx is not installed")
class WireTest(unittest.TestCase):
    def capture(self, handler=None):
        requests = []

        def record(request):
            requests.append(request)
            return handler(request) if handler else httpx.Response(200, json={"id": "x"})

        return requests, httpx.AsyncClient(transport=httpx.MockTransport(record))

    def test_a_link_event_posts_json_to_the_ntfy_root(self):
        requests, client = self.capture()

        async def go():
            async with client:
                return await main.make_poster(client, "tk_secret")(
                    main.DEPLOYMENT_COMPLETED, "t", "b", {}, message_of(deployment()))

        self.assertEqual(run(go()), (200, None))
        request = requests[0]
        self.assertEqual((request.method, str(request.url)), ("POST", f"{main.NTFY_URL}/"))
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.headers["Authorization"], "Bearer tk_secret")
        self.assertNotIn("Attach", request.headers)
        self.assertNotIn("Actions", request.headers)
        body = json.loads(request.content)
        self.assertEqual(body["topic"], main.NTFY_TOPIC)
        self.assertEqual(len(body["actions"]), 3)
        self.assertNotIn("attach", body)

    def test_an_event_without_links_is_byte_identical_to_the_old_header_publish(self):
        event = env("bloodbank.agent.session.started", summary="hello, world; ok")
        title, body = main.format_route(event, "s")
        new_requests, new_client = self.capture()
        old_requests, old_client = self.capture()

        async def go():
            async with new_client, old_client:
                router, _, _ = make(burst=10)
                router.post = main.make_poster(new_client, "tk_secret")
                await router.handle(event, "s")
                # The publish as main.py made it before links existed (2026-10-07).
                headers = {"Title": title, "Priority": main.NTFY_PRIORITY, "Tags": main.NTFY_TAGS}
                await old_client.post(f"{main.NTFY_URL}/{main.NTFY_TOPIC}",
                                      headers={**headers, "Authorization": "Bearer tk_secret"},
                                      content=body.encode("utf-8"))

        run(go())
        new, old = new_requests[0], old_requests[0]
        self.assertEqual((new.method, str(new.url), new.content), (old.method, str(old.url), old.content))
        self.assertEqual(new.headers.raw, old.headers.raw)

    def test_the_size_lookup_is_an_anonymous_head(self):
        requests, client = self.capture(lambda r: httpx.Response(200, headers={"Content-Length": "72300000"}))

        async def go():
            async with client:
                return await main.make_sizer(client)(APK_URL)

        self.assertEqual(run(go()), 72_300_000)
        self.assertEqual(requests[0].method, "HEAD")
        self.assertNotIn("Authorization", requests[0].headers)

    def test_a_size_lookup_that_is_not_a_2xx_gives_no_size(self):
        for response in (httpx.Response(403, headers={"Content-Length": "0"}), httpx.Response(404),
                         httpx.Response(200, headers={"Content-Length": "abc"})):
            _, client = self.capture(lambda r, response=response: response)

            async def go(client=client):
                async with client:
                    return await main.make_sizer(client)(APK_URL)

            self.assertIsNone(run(go()))


class PreviewTest(unittest.TestCase):
    def test_preview_shows_the_json_publish_with_the_token_redacted(self):
        shown = main.preview(deployment(), apk_bytes=72_300_000)
        self.assertEqual((shown["method"], shown["url"]), ("POST", f"{main.NTFY_URL}/"))
        self.assertEqual(shown["headers"]["Authorization"], "Bearer <NTFY_TOKEN>")
        self.assertEqual(shown["body"]["title"], "Tower of Lost Things 0.2.2 deployed")
        self.assertIn("APK 72 MB", shown["body"]["message"])

    def test_preview_shows_the_header_publish_for_an_event_without_links(self):
        shown = main.preview(env("bloodbank.agent.session.started", summary="hi"))
        self.assertEqual(shown["url"], f"{main.NTFY_URL}/{main.NTFY_TOPIC}")
        self.assertEqual(shown["headers"]["Title"], "bloodbank.agent.session.started")
        self.assertEqual(shown["body"], "src: urn:test\nsummary: hi")


if __name__ == "__main__":
    unittest.main()
