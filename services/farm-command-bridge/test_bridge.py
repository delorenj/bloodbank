"""farm-command-bridge: the contract vector, every refusal, dedupe, and PubAck-before-202."""
import asyncio
from datetime import UTC, datetime, timedelta
import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('farm_command_bridge', Path(__file__).with_name('main.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

# Milkshake docs/farm-commands.md section 2.2: the golden vector all three implementations assert.
VECTOR_KEY = 'milkshake-farm-command-test-vector'
VECTOR_BODY = ('{"accept_incomplete_reason":null,"approved_by":null,"client":"fixture-client",'
               '"client_request_id":"6f1c2b7e-3d4a-4c8e-9f10-2a3b4c5d6e7f",'
               '"command_id":"42804bf8-37ef-58d0-b9ac-ec860e028b4f","date_from":null,"date_to":null,'
               '"issued_at":"2026-11-02T14:05:09.000000Z","mode":"plan","period":"2026-10",'
               '"requested_by":"jarad@automaticai.io","schema_version":1,"session_id":"sess_8Yb2kQ"}')
VECTOR_SHA = 'dad52f96601ef8e9319d0ceff992d02764f8d4a46db1fa6a8b38b7d2b52bedcf'
VECTOR_SIG = '61aba76695b7668712579642aa50f149e2e4c2c7b0095a541facf8672813531f'
NOW = datetime(2026, 11, 2, 14, 5, 12, tzinfo=UTC)
KEY_ID = 'farm-invoice-test'
KEY = 'farm-command-test-key-0123456789abcdef0123'


def request(**changes):
    value = json.loads(VECTOR_BODY)
    value.update(changes)
    value['command_id'] = b.command_id_for(value['session_id'], value['client_request_id'])
    return value


def signed(value, key=KEY):
    raw = b.canonical(value).encode()
    return raw, 'sha256=' + b.sign(key, raw)


class FakeJS:
    def __init__(self):
        self.calls, self.fail, self.stream = [], None, 'BLOODBANK_COMMANDS'

    async def publish(self, subject, body, headers=None, timeout=None):
        if self.fail:
            raise self.fail
        self.calls.append((subject, json.loads(body), headers))
        return SimpleNamespace(stream=self.stream, seq=len(self.calls))


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = b.Archive(Path(self.temp.name) / 'archive.sqlite')
        self.js = FakeJS()
        self.bridge = b.Bridge(self.archive, {KEY_ID: KEY}, self.js, clock=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def refused(self, raw, key_id, sig):
        with self.assertRaises(b.Refusal) as caught:
            self.bridge.accept(raw, key_id, sig)
        return caught.exception.status, caught.exception.code

    def test_golden_vector(self):
        raw = VECTOR_BODY.encode()
        self.assertEqual(len(raw), 380)
        self.assertEqual(b.sha256_hex(raw), VECTOR_SHA)
        self.assertEqual(b.sign(VECTOR_KEY, raw), VECTOR_SIG)
        self.assertEqual(b.canonical(json.loads(VECTOR_BODY)), VECTOR_BODY)
        self.assertEqual(b.command_id_for('sess_8Yb2kQ', '6f1c2b7e-3d4a-4c8e-9f10-2a3b4c5d6e7f'), '42804bf8-37ef-58d0-b9ac-ec860e028b4f')
        bridge = b.Bridge(self.archive, {'k': VECTOR_KEY}, self.js, clock=lambda: NOW)
        status, event, published = bridge.accept(raw, 'k', 'sha256=' + VECTOR_SIG)
        self.assertEqual((status, published), ('new', False))
        self.assertEqual(event['data']['body_sha256'], VECTOR_SHA)
        self.assertEqual(event['data']['request'], json.loads(VECTOR_BODY))

    def test_envelope_matches_the_contract_and_schema(self):
        raw, sig = signed(request())
        _, event, _ = self.bridge.accept(raw, KEY_ID, sig)
        b.validate_envelope(event)  # the registered bloodbank.billing.invoice.generate schema
        self.assertEqual(event['subject'], 'bloodbank.cmd.billing.invoice.generate')
        self.assertEqual(event['id'], event['command_id'])
        self.assertEqual(event['actor'], {'type': 'operator', 'agent_id': 'farm:jarad@automaticai.io'})
        self.assertEqual(event['time'], '2026-11-02T14:05:12.000000Z')
        self.assertEqual(event['time'], event['data']['received_at'])
        self.assertEqual(event['data']['signature'], {'alg': 'hmac-sha256', 'key_id': KEY_ID, 'value': sig[7:]})
        self.assertEqual(event['idempotency_key'], f"billing.invoice.generate:client:fixture-client:command:{event['id']}")

    def test_signature_refusals_before_parsing(self):
        raw, sig = signed(request())
        self.assertEqual(self.refused(raw, KEY_ID, None), (401, 'SIGNATURE_MISSING'))
        self.assertEqual(self.refused(raw, KEY_ID, 'sha256=xyz'), (401, 'SIGNATURE_MISSING'))
        self.assertEqual(self.refused(raw, 'other', sig), (401, 'KEY_UNKNOWN'))
        self.assertEqual(self.refused(raw, KEY_ID, 'sha256=' + '0' * 64), (401, 'SIGNATURE_INVALID'))
        _, wrong = signed(request(), key='a-different-key-of-sufficient-length-000')
        self.assertEqual(self.refused(raw, KEY_ID, wrong), (401, 'SIGNATURE_INVALID'))
        junk = b'not json at all'
        self.assertEqual(self.refused(junk, KEY_ID, 'sha256=' + b.sign(KEY, junk)), (400, 'BODY_INVALID'))
        self.assertEqual(self.refused(b'x' * 5000, KEY_ID, sig), (413, 'BODY_TOO_LARGE'))
        self.assertEqual(self.archive.health()['archived'], 0)

    def test_canonical_fields_and_window(self):
        pretty = json.dumps(request(), indent=1).encode()
        self.assertEqual(self.refused(pretty, KEY_ID, 'sha256=' + b.sign(KEY, pretty)), (422, 'BODY_NOT_CANONICAL'))
        bad = [
            request(mode='issue'),
            request(period=None),
            request(period=None, date_from='2026-10-10', date_to='2026-10-01'),
            request(accept_incomplete_reason='late vendor bill'),  # a reason belongs to apply
            request(mode='apply', accept_incomplete_reason='late', approved_by='someone@else.io'),
            request(approved_by='jarad@automaticai.io'),
            request(requested_by='Jarad@AutomaticAI.io'),
            request(client='Not A Slug'),
            dict(request(), command_id='00000000-0000-4000-8000-000000000000'),
            dict(request(), extra=1),
        ]
        for value in bad:
            raw, sig = signed(value)
            self.assertEqual(self.refused(raw, KEY_ID, sig), (422, 'REQUEST_INVALID'), value)
        ok = request(mode='apply', accept_incomplete_reason='late vendor bill', approved_by='jarad@automaticai.io')
        raw, sig = signed(ok)
        self.assertEqual(self.bridge.accept(raw, KEY_ID, sig)[0], 'new')
        ranged = request(period=None, date_from='2026-10-01', date_to='2026-10-31', client_request_id='11111111-2222-4333-8444-555555555555')
        raw, sig = signed(ranged)
        self.assertEqual(self.bridge.accept(raw, KEY_ID, sig)[0], 'new')
        for skew in (timedelta(seconds=301), timedelta(seconds=-301)):
            stale = request(issued_at=b.stamp(NOW - skew), client_request_id='99999999-2222-4333-8444-555555555555')
            raw, sig = signed(stale)
            self.assertEqual(self.refused(raw, KEY_ID, sig), (422, 'COMMAND_EXPIRED'))

    def test_dedupe_and_conflict(self):
        raw, sig = signed(request())
        self.assertEqual(self.bridge.accept(raw, KEY_ID, sig)[0], 'new')
        self.assertEqual(self.bridge.accept(raw, KEY_ID, sig)[0], 'duplicate')
        other = request(mode='apply')  # same session + request id, different body
        raw2, sig2 = signed(other)
        self.assertEqual(self.refused(raw2, KEY_ID, sig2), (409, 'COMMAND_ID_CONFLICT'))
        self.assertEqual(self.archive.health()['archived'], 1)
        with sqlite3.connect(self.archive.path) as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE commands SET body_sha256='x'")

    def test_keys_from_env(self):
        self.assertEqual(b.keys_from_env(json.dumps({KEY_ID: KEY})), {KEY_ID: KEY})
        for text in ('{}', '[]', json.dumps({KEY_ID: 'short'})):
            with self.assertRaises(ValueError):
                b.keys_from_env(text)


class HttpTest(unittest.TestCase):
    """The real handler over a socket: 202 only after the PubAck, 503 without it, 200 duplicate."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = b.Archive(Path(self.temp.name) / 'archive.sqlite')
        self.js = FakeJS()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.bridge = b.Bridge(self.archive, {KEY_ID: KEY}, self.js, clock=lambda: NOW)
        self.server = b.ThreadingHTTPServer(('127.0.0.1', 0), b.handler(self.bridge, self.loop))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.temp.cleanup()

    def post(self, raw, headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        conn.request('POST', b.PATH, body=raw, headers={'Content-Type': 'application/json', **headers})
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_flow(self):
        raw, sig = signed(request())
        headers = {'X-Milkshake-Key-Id': KEY_ID, 'X-Milkshake-Signature': sig}
        self.assertEqual(self.post(raw, {})[0], 401)
        self.js.fail = ConnectionError()
        status, body = self.post(raw, headers)
        self.assertEqual((status, body['code']), (503, 'BUS_UNAVAILABLE'))
        self.assertEqual(self.archive.health()['unpublished'], 1)
        self.js.fail = None
        status, body = self.post(raw, headers)  # the retry publishes the archived envelope
        self.assertEqual((status, body['duplicate']), (200, True))
        self.assertEqual(len(self.js.calls), 1)
        subject, event, msg_headers = self.js.calls[0]
        self.assertEqual(subject, b.SUBJECT)
        self.assertEqual(msg_headers, {'Nats-Msg-Id': event['id']})
        self.assertEqual(self.archive.health()['unpublished'], 0)
        raw2, sig2 = signed(request(client_request_id='aaaaaaaa-2222-4333-8444-555555555555'))
        status, body = self.post(raw2, {'X-Milkshake-Key-Id': KEY_ID, 'X-Milkshake-Signature': sig2})
        self.assertEqual((status, body['success'], body['duplicate']), (202, True, False))
        self.assertEqual(body['received_at'], '2026-11-02T14:05:12.000000Z')
        self.js.stream = 'BLOODBANK_EVENTS'
        raw3, sig3 = signed(request(client_request_id='bbbbbbbb-2222-4333-8444-555555555555'))
        self.assertEqual(self.post(raw3, {'X-Milkshake-Key-Id': KEY_ID, 'X-Milkshake-Signature': sig3})[0], 503)


    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        conn.request('GET', path)
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_healthz_is_liveness_and_readiness_is_in_the_body(self):
        status, body = self.get('/healthz')
        self.assertEqual((status, body['live'], body['ready']), (200, True, True))
        self.assertEqual(self.get('/readyz')[0], 200)
        self.bridge.broker_connected = lambda: False  # a NATS blip
        status, body = self.get('/healthz')
        self.assertEqual(status, 200)  # the container stays healthy, Traefik keeps routing
        self.assertEqual((body['live'], body['ready'], body['broker_connected']), (True, False, False))
        status, body = self.get('/readyz')
        self.assertEqual((status, body['ready']), (503, False))
        self.bridge.broker_connected = lambda: True
        self.js.fail = ConnectionError()
        raw, sig = signed(request())
        self.assertEqual(self.post(raw, {'X-Milkshake-Key-Id': KEY_ID, 'X-Milkshake-Signature': sig})[0], 503)
        status, body = self.get('/healthz')
        self.assertEqual((status, body['unpublished'], body['ready']), (200, 1, False))
        self.assertEqual(body['last_error'], 'publish:ConnectionError')


class SweepTest(unittest.TestCase):
    """The sweep publishes every pending row; one that cannot publish never holds up the rest."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = b.Archive(Path(self.temp.name) / 'archive.sqlite')
        self.js = PoisonJS()
        self.connected = True
        self.bridge = b.Bridge(self.archive, {KEY_ID: KEY}, self.js,
                               broker_connected=lambda: self.connected, clock=lambda: NOW)
        self.ids = []
        for n in range(3):
            raw, sig = signed(request(client_request_id=f'cccccccc-2222-4333-8444-55555555555{n}'))
            _status, event, _published = self.bridge.accept(raw, KEY_ID, sig)
            self.ids.append(event['id'])

    def tearDown(self):
        self.temp.cleanup()

    def test_a_row_that_cannot_publish_is_skipped_and_the_rest_flow(self):
        self.js.poison = {self.ids[0]}
        asyncio.run(self.bridge.sweep())
        self.assertEqual([e['id'] for _s, e, _h in self.js.calls], self.ids[1:])
        health = self.archive.health()
        self.assertEqual((health['unpublished'], health['errors']), (1, 1))
        self.js.poison = set()
        asyncio.run(self.bridge.sweep())  # the next sweep retries only the one left
        self.assertEqual([e['id'] for _s, e, _h in self.js.calls], [*self.ids[1:], self.ids[0]])
        self.assertEqual(self.archive.health()['unpublished'], 0)

    def test_the_sweep_waits_while_the_broker_is_down(self):
        self.connected = False
        asyncio.run(self.bridge.sweep())
        self.assertEqual((self.js.attempts, self.archive.health()['unpublished']), (0, 3))


class PoisonJS(FakeJS):
    """Refuses the events whose id is in ``poison``; publishes the rest."""

    def __init__(self):
        super().__init__()
        self.poison, self.attempts = set(), 0

    async def publish(self, subject, body, headers=None, timeout=None):
        self.attempts += 1
        if json.loads(body)['id'] in self.poison:
            raise ValueError('poison row')
        return await super().publish(subject, body, headers, timeout)


if __name__ == '__main__':
    unittest.main()
