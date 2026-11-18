"""core/statekit.py and statekit_prelude.lua, against a throwaway Redis.

Every test runs on a DisposableRedis (kernel-picked port, never 6379). The
kit's frozen public API is what is under test: Script, Library, connect,
redis_url, heartbeat, heartbeat_age_ms, stream_tail, and the prelude's `sk`.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import sys
import tempfile
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from core import statekit                                   # noqa: E402
from core.resp import Connection, RedisError                # noqa: E402
from core.statekit import (Library, Script, connect, heartbeat,   # noqa: E402
                           heartbeat_age_ms, redis_url, stream_tail)
from isolated_redis import DisposableRedis                  # noqa: E402

REDIS: DisposableRedis | None = None


def setUpModule():
    global REDIS
    REDIS = DisposableRedis()
    unittest.addModuleCleanup(REDIS.close)
    REDIS.start()


class KitCase(unittest.TestCase):
    def setUp(self):
        self.conn = Connection(REDIS.url)
        self.addCleanup(self.conn.close)
        self.conn.command("FLUSHALL")
        self.conn.command("SCRIPT", "FLUSH")
        self.conn.command("FUNCTION", "FLUSH")
        self.tmp = tempfile.TemporaryDirectory(prefix="statekit-test-")
        self.addCleanup(self.tmp.cleanup)
        self.key = f"sk:{uuid.uuid4().hex}"

    def write(self, name: str, text: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def scripts_cached(self, script: Script) -> int:
        return self.conn.command("SCRIPT", "EXISTS", script.sha())[0]


class ScriptTest(KitCase):
    def test_evalsha_runs_and_caches_by_body_sha(self):
        script = Script(self.write("echo.lua", "return ARGV[1] .. ':' .. KEYS[1]"))
        self.assertEqual(self.scripts_cached(script), 0)
        self.assertEqual(script(self.conn, ["k"], ["hello"]), "hello:k")
        self.assertEqual(self.scripts_cached(script), 1)
        # the second call is a pure EVALSHA hit
        self.assertEqual(script(self.conn, ["k"], ["again"]), "again:k")

    def test_noscript_falls_back_to_load_between_calls(self):
        script = Script(self.write("echo.lua", "return ARGV[1]"))
        self.assertEqual(script(self.conn, [], ["a"]), "a")
        for n in range(3):
            self.conn.command("SCRIPT", "FLUSH")
            self.assertEqual(self.scripts_cached(script), 0)
            self.assertEqual(script(self.conn, [], [f"b{n}"]), f"b{n}")
            self.assertEqual(self.scripts_cached(script), 1)

    def test_sha_is_derived_from_prelude_plus_body_every_call(self):
        path = self.write("v.lua", "return 'one'")
        script = Script(path)
        before = script.sha()
        self.assertEqual(script(self.conn, [], []), "one")
        path.write_text("return 'two'", encoding="utf-8")
        self.assertNotEqual(script.sha(), before)
        # an edited body is a different sha, so the edit takes effect at once
        self.assertEqual(script(self.conn, [], []), "two")
        self.assertEqual(script.body(),
                         statekit.PRELUDE.read_bytes() + b"\n" + b"return 'two'")

    def test_prelude_false_ships_the_bare_body(self):
        path = self.write("bare.lua", "return type(sk)")
        self.assertEqual(Script(path, prelude=False).body(), path.read_bytes())
        with self.assertRaises(RedisError) as ctx:       # no `sk` global without the prelude
            Script(path, prelude=False)(self.conn, [], [])
        self.assertIn("sk", str(ctx.exception))
        self.assertEqual(Script(path)(self.conn, [], []), "table")
        self.assertNotEqual(Script(path).sha(), Script(path, prelude=False).sha())

    def test_other_errors_propagate_and_are_not_retried(self):
        script = Script(self.write("boom.lua", "return redis.error_reply('boom')"))
        with self.assertRaises(RedisError) as ctx:
            script(self.conn, [], [])
        self.assertIn("boom", str(ctx.exception))
        self.assertNotIn("NOSCRIPT", str(ctx.exception))


class LibraryTest(KitCase):
    BODY = ("redis.register_function('{n}_ping', function(keys, args)\n"
            "  return 'pong:' .. (args[1] or '')\n"
            "end)\n"
            "redis.register_function{{function_name='{n}_ro', flags={{'no-writes'}},\n"
            "  callback=function(keys, args) return sk.str(nil) .. 'ro' end}}\n"
            "redis.register_function('{n}_set', function(keys, args)\n"
            "  redis.call('SET', keys[1], args[1]); return sk.now_ms() > 0\n"
            "end)\n")

    def lib(self, name="demo", body=None) -> Library:
        return Library(name, self.write(f"{name}.lua", body or self.BODY.format(n=name)))

    def test_ensure_loads_once_then_is_a_noop(self):
        lib = self.lib()
        self.assertIsNone(lib.loaded_version(self.conn))
        self.assertIs(lib.ensure(self.conn), True)
        self.assertIs(lib.ensure(self.conn), False)
        self.assertEqual(lib.loaded_version(self.conn), lib.version())

    def test_version_function_is_fcall_ro_and_matches_version(self):
        lib = self.lib()
        lib.ensure(self.conn)
        got = self.conn.command("FCALL_RO", "demo_version", 0)
        self.assertEqual(got, lib.version())
        self.assertEqual(len(got), 40)

    def test_version_changes_when_the_body_file_changes(self):
        lib = self.lib()
        lib.ensure(self.conn)
        v1 = lib.version()
        lib.path.write_text(lib.path.read_text() + "\n-- edit\n", encoding="utf-8")
        self.assertNotEqual(lib.version(), v1)
        self.assertNotEqual(lib.loaded_version(self.conn), lib.version())
        self.assertIs(lib.ensure(self.conn), True)      # REPLACE picks up the edit
        self.assertEqual(lib.loaded_version(self.conn), lib.version())
        self.assertIs(lib.ensure(self.conn), False)

    def test_code_is_header_prelude_body_and_version_function(self):
        lib = self.lib()
        code = lib.code()
        self.assertTrue(code.startswith("#!lua name=demo\n"))
        self.assertIn("function sk.iso(", code)
        self.assertIn(self.BODY.format(n="demo"), code)
        self.assertIn("demo_version", code)
        self.assertIn(lib.version(), code)

    def test_call_autoloads_after_function_flush(self):
        lib = self.lib()
        self.assertEqual(lib.call(self.conn, "demo_ping", [], ["x"]), "pong:x")  # never loaded
        self.assertIs(lib.ensure(self.conn), False)
        self.conn.command("FUNCTION", "FLUSH")
        self.assertIsNone(lib.loaded_version(self.conn))
        self.assertEqual(lib.call(self.conn, "demo_ping", [], ["y"]), "pong:y")
        self.assertEqual(lib.loaded_version(self.conn), lib.version())

    def test_call_ro_and_keys_and_prelude_inside_a_function(self):
        lib = self.lib()
        self.assertEqual(lib.call(self.conn, "demo_ro", ro=True), "ro")
        self.assertEqual(lib.call(self.conn, "demo_set", [self.key], ["v"]), 1)
        self.assertEqual(self.conn.command("GET", self.key), "v")
        self.conn.command("FUNCTION", "FLUSH")
        self.assertEqual(lib.call(self.conn, "demo_ro", ro=True), "ro")   # RO autoloads too

    def test_call_does_not_swallow_other_errors(self):
        lib = self.lib()
        lib.ensure(self.conn)
        with self.assertRaises(RedisError) as ctx:
            lib.call(self.conn, "demo_ping", ["a", "b"], ["extra"], ro=True)   # write fn via RO
        self.assertNotIn("not found", str(ctx.exception).lower())

    def test_a_function_that_never_existed_fails_after_one_retry(self):
        lib = self.lib()
        with self.assertRaises(RedisError):
            lib.call(self.conn, "demo_nope")
        self.assertEqual(lib.loaded_version(self.conn), lib.version())   # it did load once

    def test_not_found_text_is_what_redis_says_for_both_verbs(self):
        # The anchor in Library.call is only as good as this: pin the server text.
        for verb in ("FCALL", "FCALL_RO"):
            with self.assertRaises(RedisError) as ctx:
                self.conn.command(verb, "demo_absent", 0)
            self.assertTrue(str(ctx.exception).startswith("ERR Function not found"), str(ctx.exception))

    def test_a_write_function_whose_own_error_says_not_found_runs_once(self):
        # Regression (asm-2): a substring match on "function not found" took the
        # function's OWN error for a missing function, reloaded, and ran the
        # write a second time.
        body = ("redis.register_function('demo_once', function(keys, args)\n"
                "  redis.call('INCR', keys[1])\n"
                "  return redis.error_reply('lookup failed: function not found in registry')\n"
                "end)\n")
        lib = self.lib(body=body)
        lib.ensure(self.conn)
        with self.assertRaises(RedisError) as ctx:
            lib.call(self.conn, "demo_once", [self.key])
        self.assertIn("function not found in registry", str(ctx.exception))
        self.assertEqual(self.conn.command("GET", self.key), "1")        # not "2"
        with self.assertRaises(RedisError):
            lib.call(self.conn, "demo_once", [self.key], ro=False)
        self.assertEqual(self.conn.command("GET", self.key), "2")

    def test_two_libraries_coexist(self):
        a, b = self.lib("alpha"), self.lib("beta")
        a.ensure(self.conn), b.ensure(self.conn)
        self.assertEqual(a.call(self.conn, "alpha_ping", [], ["1"]), "pong:1")
        self.assertEqual(b.call(self.conn, "beta_ping", [], ["2"]), "pong:2")
        self.assertEqual(self.conn.command("FCALL_RO", "beta_version", 0), b.version())


class PreludeTest(KitCase):
    def run_lua(self, body: str, keys=(), args=()):
        script = Script(self.write(f"p{uuid.uuid4().hex[:6]}.lua", body))
        return script(self.conn, list(keys), list(args))

    def test_iso_known_values(self):
        got = json.loads(self.run_lua(
            "local out = {} for i, ms in ipairs(cjson.decode(ARGV[1])) do out[i] = sk.iso(ms) end "
            "return cjson.encode(out)",
            args=[json.dumps([0, 1709210096000, 1795021445513, 946684799000,
                              951782400000, 4102444799000])]))
        self.assertEqual(got, [
            "1970-01-01T00:00:00Z",
            "2024-02-29T12:34:56Z",
            "2026-11-18T17:04:05Z",
            "1999-12-31T23:59:59Z",
            "2000-02-29T00:00:00Z",      # leap day of a 400-year leap year
            "2099-12-31T23:59:59Z",
        ])

    def test_iso_round_trips_against_python(self):
        rng = random.Random(33)
        mss = [rng.randrange(0, 4_200_000_000_000) for _ in range(300)]
        mss += [86_399_999, 86_400_000, 951_782_399_999, 1_078_099_200_000]   # day/leap edges
        got = json.loads(self.run_lua(
            "local out = {} for i, ms in ipairs(cjson.decode(ARGV[1])) do out[i] = sk.iso(ms) end "
            "return cjson.encode(out)", args=[json.dumps(mss)]))
        want = [dt.datetime.fromtimestamp(ms // 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                for ms in mss]
        self.assertEqual(got, want)
        parsed = dt.datetime.strptime(got[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        self.assertEqual(int(parsed.timestamp()), mss[0] // 1000)

    def test_str_coerces_every_json_shape(self):
        got = json.loads(self.run_lua(
            "local m = cjson.decode(ARGV[1]) "
            "return cjson.encode({sk.str(nil), sk.str(cjson.null), sk.str(m.n), sk.str(true), "
            "sk.str(false), sk.str(m.i), sk.str(m.f), sk.str(m.w), sk.str(m.s), sk.str({}), "
            "sk.str(m.neg)})",
            args=['{"n": null, "i": 42, "f": 1.5, "w": 3.0, "s": "text", "neg": -7}']))
        self.assertEqual(got, ["", "", "", "1", "0", "42", "1.5", "3", "text", "", "-7"])

    def test_str_numbers_nan_inf_big_and_fractions(self):
        # Regression (asm-3): '%d' of inf/2^64 is garbage and tostring() is %.14g.
        got = json.loads(self.run_lua(
            "local nan, inf = 0/0, math.huge "
            "return cjson.encode({sk.str(nan), sk.str(inf), sk.str(-inf), "
            "sk.str(2^53 - 1), sk.str(-(2^53 - 1)), sk.str(2^53), sk.str(1e20), "
            "sk.str(-1e20), sk.str(0.1), sk.str(1/3), sk.str(-0.0), sk.str(0), sk.str(1.5)})"))
        self.assertEqual(got, ["", "", "", "9007199254740991", "-9007199254740991",
                               "9007199254740992", "1e+20", "-1e+20",
                               "0.10000000000000001", "0.33333333333333331", "0", "0", "1.5"])

    def test_str_fraction_round_trips_exactly(self):
        for v in (0.1, 1 / 3, 123456.789012345678, 2.0 ** -40, 1.7976931348623157e308):
            got = self.run_lua("return sk.str(tonumber(ARGV[1]))", args=[repr(v)])
            self.assertEqual(float(got), v, got)

    def test_str_makes_a_null_safe_redis_argument(self):
        # The reason sk.str exists: a bare cjson.null aborts the whole script.
        self.run_lua("local m = cjson.decode(ARGV[1]) redis.call('HSET', KEYS[1], 'a', sk.str(m.a), 'b', sk.str(m.b)) return 1",
                     keys=[self.key], args=['{"a": null, "b": 5}'])
        self.assertEqual(self.conn.command("HGET", self.key, "a"), "")
        self.assertEqual(self.conn.command("HGET", self.key, "b"), "5")

    def test_present_hgetall_hset_json_list(self):
        got = json.loads(self.run_lua(
            "local m = cjson.decode(ARGV[1]) "
            "sk.hset(KEYS[1], {a = 1, b = cjson.null, c = 'x', d = 2.5}) "
            "sk.hset(KEYS[1], {}) "
            "local h = sk.hgetall(KEYS[1]) "
            "return cjson.encode({sk.present(nil), sk.present(m.z), sk.present(m.y), h.a, h.b, h.c, h.d, "
            "sk.json_list({}), sk.json_list({1, 2}), next(sk.hgetall('absent:' .. KEYS[1])) == nil})",
            keys=[self.key], args=['{"z": null, "y": 0}']))
        self.assertEqual(got, [False, False, True, "1", "", "x", "2.5", "[]", "[1,2]", True])

    def test_now_ms_is_the_server_clock(self):
        lo = int(time.time() * 1000)
        got = int(self.run_lua("return sk.str(sk.now_ms())"))
        hi = int(time.time() * 1000)
        self.assertTrue(lo - 2000 <= got <= hi + 2000, (lo, got, hi))

    def test_emit_appends_to_a_capped_stream_and_publishes(self):
        stream, channel = f"{self.key}:t", f"{self.key}:chan"
        with Connection(REDIS.url) as sub:
            self.assertEqual(sub.command("SUBSCRIBE", channel), ["subscribe", channel, 1])
            sub._sock.settimeout(3.0)
            first = self.run_lua(
                "return sk.emit(KEYS[1], 10, {'j', ARGV[1], 'n', 7, 'nul', cjson.null}, ARGV[2], ARGV[1])",
                keys=[stream], args=['{"edge":1}', channel])
            self.assertRegex(first, r"^\d+-\d+$")
            self.assertEqual(sub._read_reply(), ["message", channel, '{"edge":1}'])
        self.assertEqual(self.conn.command("XRANGE", stream, "-", "+")[0][1],
                         ["j", '{"edge":1}', "n", "7", "nul", ""])
        # MAXLEN ~ 10: radix-node trimming is lazy, but 400 entries cannot all survive
        for i in range(400):
            self.run_lua("sk.emit(KEYS[1], 10, {'i', ARGV[1]}, nil, nil)", keys=[stream], args=[i])
        self.assertLess(self.conn.command("XLEN", stream), 400)
        self.assertGreaterEqual(self.conn.command("XLEN", stream), 10)

    def test_emit_with_no_channel_or_stream_skips_that_half(self):
        stream = f"{self.key}:t"
        self.assertIsNone(self.run_lua("return sk.emit(nil, 5, {'a', 'b'}, nil, nil)") or None)
        self.assertEqual(self.conn.command("DBSIZE"), 0)
        self.run_lua("sk.emit(KEYS[1], 5, {'a', 'b'}, '', 'ignored')", keys=[stream])
        self.assertEqual(self.conn.command("XLEN", stream), 1)

    def test_heartbeat_sets_ts_and_fields_and_ttl(self):
        lo = int(time.time() * 1000)
        ts = int(self.run_lua(
            "return sk.str(sk.heartbeat(KEYS[1], 5000, {by = 'sweeper', n = 3, nul = cjson.null}))",
            keys=[self.key]))
        row = dict(zip(*(iter(self.conn.command("HGETALL", self.key)),) * 2))
        self.assertEqual(int(row["ts"]), ts)
        self.assertTrue(lo - 2000 <= ts <= lo + 5000)
        self.assertEqual((row["by"], row["n"], row["nul"]), ("sweeper", "3", ""))
        self.assertTrue(0 < self.conn.command("PTTL", self.key) <= 5000)

    def test_heartbeat_without_ttl_does_not_expire(self):
        self.run_lua("sk.heartbeat(KEYS[1], 0, nil) sk.heartbeat(KEYS[1] .. ':b', nil, {}) return 1",
                     keys=[self.key])
        self.assertEqual(self.conn.command("PTTL", self.key), -1)
        self.assertEqual(self.conn.command("PTTL", self.key + ":b"), -1)


class PythonHelpersTest(KitCase):
    def test_heartbeat_then_age(self):
        self.assertIsNone(heartbeat_age_ms(self.conn, self.key))          # absent -> None, never 0
        ts = heartbeat(self.conn, self.key, 60_000, by="pytest", panes=4)
        self.assertAlmostEqual(ts, time.time() * 1000, delta=2000)
        age = heartbeat_age_ms(self.conn, self.key)
        self.assertTrue(0 <= age < 2000, age)
        self.assertEqual(heartbeat_age_ms(self.conn, self.key, now_ms=ts + 12_345), 12_345)
        self.assertEqual(self.conn.command("HGET", self.key, "by"), "pytest")
        self.assertEqual(self.conn.command("HGET", self.key, "panes"), "4")
        self.assertTrue(0 < self.conn.command("PTTL", self.key) <= 60_000)

    def test_heartbeat_expires_to_none(self):
        heartbeat(self.conn, self.key, 40)
        time.sleep(0.15)
        self.assertIsNone(heartbeat_age_ms(self.conn, self.key))

    def test_heartbeat_without_ttl_persists(self):
        heartbeat(self.conn, self.key)
        self.assertEqual(self.conn.command("PTTL", self.key), -1)

    def test_heartbeat_age_none_on_a_garbage_ts(self):
        self.conn.command("HSET", self.key, "ts", "not-a-number")
        self.assertIsNone(heartbeat_age_ms(self.conn, self.key))

    def test_stream_tail_is_oldest_first_and_capped(self):
        self.assertEqual(stream_tail(self.conn, self.key), [])
        for i in range(5):
            self.conn.command("XADD", self.key, "*", "i", i, "tag", f"t{i}")
        tail = stream_tail(self.conn, self.key, 3)
        self.assertEqual([r["i"] for r in tail], ["2", "3", "4"])
        self.assertEqual(tail[0]["tag"], "t2")
        self.assertRegex(tail[0]["id"], r"^\d+-\d+$")
        self.assertEqual([r["i"] for r in stream_tail(self.conn, self.key, 50)],
                         ["0", "1", "2", "3", "4"])
        ids = [r["id"] for r in stream_tail(self.conn, self.key, 5)]
        self.assertEqual(ids, sorted(ids, key=lambda s: tuple(map(int, s.split("-")))))

    def test_connect_and_redis_url(self):
        with connect(REDIS.url) as conn:
            self.assertEqual(conn.command("PING"), "PONG")
        env = {"SK_TEST_URL": REDIS.url}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            self.assertEqual(redis_url("SK_TEST_URL"), REDIS.url)
            self.assertEqual(redis_url("SK_TEST_MISSING", "SK_TEST_URL"), REDIS.url)
        with unittest.mock.patch.dict("os.environ", {"REDIS_URL": "redis://h:1"}, clear=False):
            self.assertEqual(redis_url("SK_TEST_MISSING"), "redis://h:1")
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(redis_url("SK_TEST_MISSING"), "redis://127.0.0.1:6379")
            self.assertEqual(redis_url(default="redis://d:2"), "redis://d:2")

    def test_the_frozen_surface_is_exported(self):
        for name in ("Script", "Library", "connect", "redis_url", "heartbeat",
                     "heartbeat_age_ms", "stream_tail"):
            self.assertIn(name, statekit.__all__)


if __name__ == "__main__":
    unittest.main()
