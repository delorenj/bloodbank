"""farm-command-bridge: the contract vector, every refusal, dedupe, and PubAck-before-202."""
import asyncio
from datetime import UTC, datetime, timedelta
import http.client
import importlib.util
import re
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import hashlib
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
        bridge = b.Bridge(self.archive, {'farm-invoice-vector': VECTOR_KEY}, self.js, clock=lambda: NOW)
        status, event, published = bridge.accept(raw, 'farm-invoice-vector', 'sha256=' + VECTOR_SIG)
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

    def test_a_pending_read_failure_does_not_kill_the_next_sweep(self):
        from unittest.mock import patch

        real = self.archive.pending
        calls = 0

        def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('archive temporarily unavailable')
            return real()

        async def exercise():
            with patch.object(self.archive, 'pending', side_effect=flaky), patch.object(b.asyncio, 'sleep') as backoff:
                await self.bridge.sweep()
                backoff.assert_awaited_once_with(15)
                await self.bridge.sweep()

        asyncio.run(exercise())
        self.assertEqual(self.archive.health()['unpublished'], 0)
        self.assertEqual(calls, 2)

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


# ---------------------------------------------------------------------------------------------
# The command palette (Milkshake Story 10.3, docs/farm-commands.md section 14).

FIXTURE = Path(__file__).with_name('fixtures') / 'farm-palette-vectors.json'
FIXTURE_SHA = '8b986c905a2f660c2f28399cf89cde28d7e020ac803d5d9fbe5d4042c00f2bf9'
VECTORS = json.loads(FIXTURE.read_text(encoding='utf-8'))
PALETTE_KEY_ID = VECTORS['key_id']  # farm-command-test
PALETTE_KEY = VECTORS['key']
PALETTE_NOW = datetime(2026, 11, 2, 14, 5, 10, 123456, tzinfo=UTC)  # the envelope vector's received_at
KEYS = {KEY_ID: KEY, PALETTE_KEY_ID: PALETTE_KEY}
SQUEEZE = VECTORS['vectors'][0]
BY_NAME = {v['name']: v for v in VECTORS['vectors']}
RULE_MODE = {'squeeze': 'plan', 'boi review': 'apply', 'rule revoke': 'apply', 'yrd payment': 'plan', 'farm push': 'plan'}
SUBJECT_OF = {v['command']: v['subject'] for v in VECTORS['vectors']}


def palette_request(base=SQUEEZE, **changes):
    """A v2 request around a vector's, with command_id re-derived (changes win over the base)."""
    value = json.loads(base['body'])
    value.update(changes)
    value['command_id'] = b.command_id_for(value['session_id'], value['client_request_id'])
    return value


def palette_signed(value, key=PALETTE_KEY):
    """The Worker's bytes: canonical, with a lone surrogate spelled as the JSON escape JSON.stringify emits."""
    raw = b.canonical(value).encode('utf-8', errors='backslashreplace')
    return raw, 'sha256=' + b.sign(key, raw)


def args_case_request(case, number=0):
    args = json.loads(case['args_json'])
    repeat = case.get('reason_repeat')
    if repeat:
        args['reason'] = repeat['unit'] * repeat['times']
    command = case['command']
    return palette_request(
        command=command, subject=SUBJECT_OF[command], mode=RULE_MODE[command], client=case['client'], args=args,
        client_request_id=f'00000000-0000-4000-8000-{number:012d}')  # one id per case: one archive row each


class PaletteFixtureTest(unittest.TestCase):
    def test_the_fixture_is_the_contract_file(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (20067, FIXTURE_SHA))

    def test_the_five_vectors_reproduce(self):
        self.assertEqual([v['name'] for v in VECTORS['vectors']],
                         ['squeeze-plan', 'boi-review-apply', 'rule-revoke-apply', 'yrd-payment-plan', 'farm-push-plan'])
        for vector in VECTORS['vectors']:
            raw = vector['body'].encode('utf-8')
            self.assertEqual(len(raw), vector['body_bytes'], vector['name'])
            self.assertEqual(b.sha256_hex(raw), vector['body_sha256'], vector['name'])
            self.assertEqual('sha256=' + b.sign(PALETTE_KEY, raw), vector['signature'], vector['name'])
            self.assertEqual(b.command_id_for(VECTORS['session_id'], vector['client_request_id']), vector['command_id'])
            parsed = json.loads(vector['body'])
            self.assertEqual(b.canonical(parsed), vector['body'], vector['name'])
            self.assertEqual(b.sha256_hex(b.canonical(parsed['args']).encode()), vector['args_sha256'], vector['name'])
            self.assertEqual(ROUTES_OF[vector['subject']], vector['command'])

    def test_the_longest_run_actor_fits_the_ledger_schemas(self):
        # farm:<email>/<command_id> is announced as data.actor, which the billing schemas cap at 200.
        actor = 'farm:' + 'a' * b.PALETTE_MAX_EMAIL + '/' + SQUEEZE['command_id']
        self.assertEqual(b.PALETTE_MAX_EMAIL, 158)
        self.assertLessEqual(len(actor), 200)
        self.assertEqual(len(actor), 200)


ROUTES_OF = b.ROUTES


class PaletteBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = b.Archive(Path(self.temp.name) / 'archive.sqlite')
        self.js = FakeJS()
        self.bridge = b.Bridge(self.archive, KEYS, self.js, clock=lambda: PALETTE_NOW)

    def tearDown(self):
        self.temp.cleanup()

    def refused(self, raw, key_id, sig, accept=None):
        with self.assertRaises(b.Refusal) as caught:
            (accept or self.bridge.accept_palette)(raw, key_id, sig)
        return caught.exception.status, caught.exception.code

    def test_routes_are_the_five_registered_subjects(self):
        self.assertEqual(b.ROUTES, {
            'bloodbank.cmd.billing.cost.receive': 'squeeze',
            'bloodbank.cmd.billing.cost.close': 'boi review',
            'bloodbank.cmd.billing.rule.cancel': 'rule revoke',
            'bloodbank.cmd.billing.payment.receive': 'yrd payment',
            'bloodbank.cmd.billing.snapshot.send': 'farm push',
        })

    def test_every_vector_is_accepted_and_validates_as_its_schema(self):
        for vector in VECTORS['vectors']:
            raw = vector['body'].encode()
            status, event, published = self.bridge.accept_palette(raw, PALETTE_KEY_ID, vector['signature'])
            self.assertEqual((status, published), ('new', False), vector['name'])
            b.validate_envelope(event)
            self.assertEqual(event['subject'], vector['subject'])
            self.assertEqual(event['type'], vector['subject'].replace('.cmd.', '.'))
            self.assertEqual(event['id'], vector['command_id'])
            self.assertEqual(event['data']['request'], json.loads(vector['body']))
            self.assertEqual(event['data']['body_sha256'], vector['body_sha256'])
            self.assertEqual(event['data']['signature'], {'alg': 'hmac-sha256', 'key_id': PALETTE_KEY_ID, 'value': vector['signature'][7:]})
            entity_verb = vector['subject'].removeprefix('bloodbank.cmd.')
            self.assertEqual(event['idempotency_key'], f"{entity_verb}:client:fixture-client:command:{vector['command_id']}")

    def test_the_squeeze_envelope_is_the_vectors_bytes(self):
        envelope = VECTORS['envelope']
        self.assertEqual(envelope['for'], 'squeeze-plan')
        self.assertEqual(b.stamp(PALETTE_NOW), envelope['received_at'])
        _, event, _ = self.bridge.accept_palette(SQUEEZE['body'].encode(), PALETTE_KEY_ID, SQUEEZE['signature'])
        encoded = b.canonical(event).encode('utf-8')
        self.assertEqual(len(encoded), envelope['bytes'])
        self.assertEqual(len(encoded), 1569)
        self.assertEqual(b.sha256_hex(encoded), envelope['sha256'])
        self.assertEqual(event['actor'], {'type': 'operator', 'agent_id': 'farm:jarad@automaticai.io'})
        self.assertEqual(event['time'], event['data']['received_at'])
        self.assertEqual(event['causationid'], None)
        self.assertEqual(event['delivery'], 'single_consumer')

    def test_key_purpose_both_ways(self):
        raw, sig = signed(request())  # a v1 body signed with the invoice key
        self.assertEqual(self.refused(raw, KEY_ID, sig), (401, 'KEY_UNKNOWN'))  # invoice key on /v1/commands
        vraw = SQUEEZE['body'].encode()
        self.assertEqual(self.refused(vraw, PALETTE_KEY_ID, SQUEEZE['signature'], self.bridge.accept), (401, 'KEY_UNKNOWN'))
        self.assertEqual(self.bridge.accept(raw, KEY_ID, sig)[0], 'new')  # each key on its own route

    def test_the_401_order_on_both_routes(self):
        for accept, key_id, key, body in (
            (self.bridge.accept_palette, PALETTE_KEY_ID, PALETTE_KEY, SQUEEZE['body'].encode()),
            (self.bridge.accept, KEY_ID, KEY, signed(request())[0]),
        ):
            good = 'sha256=' + b.sign(key, body)
            self.assertEqual(self.refused(body, None, None, accept), (401, 'SIGNATURE_MISSING'))  # neither header
            self.assertEqual(self.refused(body, key_id, 'sha256=xyz', accept), (401, 'SIGNATURE_MISSING'))
            self.assertEqual(self.refused(body, None, good, accept), (401, 'KEY_UNKNOWN'))
            self.assertEqual(self.refused(body, 'unknown-key', good, accept), (401, 'KEY_UNKNOWN'))  # well-formed, unknown id
            self.assertEqual(self.refused(body, key_id, 'sha256=' + '0' * 64, accept), (401, 'SIGNATURE_INVALID'))  # known key, wrong HMAC
            self.assertEqual(self.refused(body, 'unknown-key', 'sha256=xyz', accept), (401, 'SIGNATURE_MISSING'))
        self.assertEqual(self.refused(b'x' * 5000, PALETTE_KEY_ID, SQUEEZE['signature']), (413, 'BODY_TOO_LARGE'))
        self.assertEqual(self.archive.health()['archived'], 0)

    def test_unknown_subject_and_request_refusals(self):
        def refuse(code, status=422, **changes):
            raw, sig = palette_signed(palette_request(**changes))
            self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (status, code), changes)
        refuse('SUBJECT_UNKNOWN', subject='bloodbank.cmd.billing.invoice.generate')
        refuse('SUBJECT_UNKNOWN', subject='bloodbank.cmd.billing.cost.observe')
        refuse('REQUEST_INVALID', subject=['not', 'a', 'string'])
        refuse('REQUEST_INVALID', command='boi review')  # not the subject's command
        refuse('REQUEST_INVALID', args={'client': 'other-client', 'period': '2026-10'})  # args.client mismatch
        refuse('REQUEST_INVALID', args={'client': 'fixture-client', 'period': '2026-10', 'vendor': 'a\x07b'})  # control char
        refuse('REQUEST_INVALID', args={'client': 'fixture-client', 'period': '2026-10', 'refresh': False})  # false flag
        refuse('REQUEST_INVALID', args={'client': 'fixture-client'})  # span: none
        refuse('REQUEST_INVALID', args={'client': 'fixture-client', 'period': '2026-10', 'date_from': '2026-10-01', 'date_to': '2026-10-31'})
        refuse('REQUEST_INVALID', args={'client': 'fixture-client', 'period': '2026-10', 'apply': True})  # unknown argument
        refuse('REQUEST_INVALID', args={'client': 'fixture-client', 'date_from': '2026-02-30', 'date_to': '2026-03-01'})  # not a day
        refuse('REQUEST_INVALID', args={'client': 'fixture-client', 'date_from': '20261001', 'date_to': '2026-10-31'})
        refuse('REQUEST_INVALID', args=['not', 'an', 'object'])
        refuse('REQUEST_INVALID', requested_by='Jarad@AutomaticAI.io')
        refuse('REQUEST_INVALID', requested_by=' jarad@automaticai.io')
        refuse('REQUEST_INVALID', requested_by='a' * 150 + '@example.io')  # 161 characters
        refuse('REQUEST_INVALID', mode='issue')
        refuse('REQUEST_INVALID', client='Not A Slug')
        refuse('REQUEST_INVALID', client='fixture-client\n', args={'client': 'fixture-client\n', 'period': '2026-10'})
        refuse('REQUEST_INVALID', issued_at='2026-13-45T25:61:61.000000Z')  # a real instant, not just digits
        refuse('REQUEST_INVALID', schema_version=1)
        refuse('REQUEST_INVALID', schema_version=True)
        refuse('REQUEST_INVALID', schema_version=2.0)
        refuse('REQUEST_INVALID', client_request_id='0B8F2D1E-6A4C-4F3B-9E2D-1C0A9B8E7D61')
        raw, sig = palette_signed(dict(palette_request(), command_id='00000000-0000-4000-8000-000000000000'))
        self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'REQUEST_INVALID'))
        raw, sig = palette_signed(dict(palette_request(), extra=1))
        self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'REQUEST_INVALID'))
        raw, sig = palette_signed({k: v for k, v in palette_request().items() if k != 'args'})
        self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'REQUEST_INVALID'))
        self.assertEqual(self.archive.health()['archived'], 0)

    def test_requested_by_is_capped_at_158_characters(self):
        tail = '@example.io'
        ok = 'a' * (158 - len(tail)) + tail
        self.assertEqual(len(ok), 158)
        self.assertEqual(self.bridge.accept_palette(*palette_signed(palette_request(requested_by=ok))[:1], PALETTE_KEY_ID, palette_signed(palette_request(requested_by=ok))[1])[0], 'new')
        too_long = 'a' * (159 - len(tail)) + tail
        self.assertEqual(len(too_long), 159)
        raw, sig = palette_signed(palette_request(requested_by=too_long, client_request_id='11111111-2222-4333-8444-555555555555'))
        self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'REQUEST_INVALID'))

    def test_a_day_must_be_a_real_calendar_date(self):
        ok = palette_request(args={'client': 'fixture-client', 'date_from': '2028-02-29', 'date_to': '2028-03-01'})
        self.assertEqual(self.bridge.accept_palette(*palette_signed(ok)[:1], PALETTE_KEY_ID, palette_signed(ok)[1])[0], 'new')
        for start, end in (('2026-02-30', '2026-03-01'), ('2026-02-29', '2026-03-01'), ('2026-04-31', '2026-05-01'), ('2026-10-01', '2026-11-31')):
            bad = palette_request(args={'client': 'fixture-client', 'date_from': start, 'date_to': end}, client_request_id='22222222-2222-4333-8444-555555555555')
            raw, sig = palette_signed(bad)
            self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'REQUEST_INVALID'), (start, end))

    def test_the_47_args_cases(self):
        cases = VECTORS['args_cases']
        self.assertEqual(len(cases), 47)
        for number, case in enumerate(cases):
            value = args_case_request(case, number)
            raw, sig = palette_signed(value)
            # The verdict is the whole accept pipeline: check_palette_request (rules 2 to 5, real
            # dates, `client`) and then the subject's JSON Schema (rule 1, the shapes).
            try:
                b.check_palette_request(value)
                function_ok = True
            except b.Refusal:
                function_ok = False
            try:
                self.bridge.accept_palette(raw, PALETTE_KEY_ID, sig)
                accepted = True
            except b.Refusal as refusal:
                accepted = False
                self.assertEqual((refusal.status, refusal.code), (422, 'REQUEST_INVALID'), case['name'])
            self.assertEqual(accepted, case['valid'], case['name'])
            if case['valid']:
                self.assertTrue(function_ok, case['name'])
        self.assertEqual(self.archive.health()['archived'], sum(1 for c in cases if c['valid']))

    def test_the_args_cases_decided_by_rules_2_to_5_are_decided_in_code(self):
        # check_palette_request is not the JSON Schema: rules 2 to 5 and the real calendar date.
        for number, case in enumerate(VECTORS['args_cases']):
            if case['rule'] in ('2', '3', '4', '5') or case['rule'].startswith('1 (DAY)'):
                value = args_case_request(case, number)
                if case['valid']:
                    b.check_palette_request(value)
                else:
                    with self.assertRaises(b.Refusal, msg=case['name']) as caught:
                        b.check_palette_request(value)
                    self.assertEqual(caught.exception.code, 'REQUEST_INVALID')

    def test_a_lone_surrogate_is_422_not_a_dropped_connection(self):
        value = palette_request(command='rule revoke', subject=SUBJECT_OF['rule revoke'], mode='apply', args={'rule_id': 'r42', 'reason': 'a\ud800b'})
        raw, sig = palette_signed(value)
        self.assertIn(b'\\ud800', raw)  # spelled as the JSON escape, the way JSON.stringify sends it
        self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'REQUEST_INVALID'))
        v1 = request()
        v1['session_id'] = 'sess_\udc00'
        raw1 = b.canonical(v1).encode('utf-8', errors='backslashreplace')
        self.assertEqual(self.refused(raw1, KEY_ID, 'sha256=' + b.sign(KEY, raw1), self.bridge.accept), (422, 'REQUEST_INVALID'))
        self.assertEqual(self.archive.health()['archived'], 0)
        # a valid pair is one code point and passes
        pair = palette_request(command='rule revoke', subject=SUBJECT_OF['rule revoke'], mode='apply', args={'rule_id': 'r42', 'reason': 'a\U0001f600b'})
        raw, sig = palette_signed(pair)
        self.assertEqual(self.bridge.accept_palette(raw, PALETTE_KEY_ID, sig)[0], 'new')

    def test_canonical_window_dedupe_and_conflict(self):
        pretty = json.dumps(palette_request(), indent=1).encode()
        self.assertEqual(self.refused(pretty, PALETTE_KEY_ID, 'sha256=' + b.sign(PALETTE_KEY, pretty)), (422, 'BODY_NOT_CANONICAL'))
        junk = b'not json at all'
        self.assertEqual(self.refused(junk, PALETTE_KEY_ID, 'sha256=' + b.sign(PALETTE_KEY, junk)), (400, 'BODY_INVALID'))
        dup_keys = b'{"a":1,"a":2}'
        self.assertEqual(self.refused(dup_keys, PALETTE_KEY_ID, 'sha256=' + b.sign(PALETTE_KEY, dup_keys)), (400, 'BODY_INVALID'))
        for skew in (timedelta(seconds=301), timedelta(seconds=-301)):
            stale = palette_request(issued_at=b.stamp(PALETTE_NOW - skew), client_request_id='99999999-2222-4333-8444-555555555555')
            raw, sig = palette_signed(stale)
            self.assertEqual(self.refused(raw, PALETTE_KEY_ID, sig), (422, 'COMMAND_EXPIRED'))
        edge = palette_request(issued_at=b.stamp(PALETTE_NOW - timedelta(seconds=300)), client_request_id='88888888-2222-4333-8444-555555555555')
        self.assertEqual(self.bridge.accept_palette(*palette_signed(edge)[:1], PALETTE_KEY_ID, palette_signed(edge)[1])[0], 'new')
        raw, sig = SQUEEZE['body'].encode(), SQUEEZE['signature']
        self.assertEqual(self.bridge.accept_palette(raw, PALETTE_KEY_ID, sig)[0], 'new')
        self.assertEqual(self.bridge.accept_palette(raw, PALETTE_KEY_ID, sig)[0], 'duplicate')
        other = palette_request(mode='apply')  # same session and request id, different bytes
        raw2, sig2 = palette_signed(other)
        self.assertEqual(self.refused(raw2, PALETTE_KEY_ID, sig2), (409, 'COMMAND_ID_CONFLICT'))

    def test_one_command_id_space_across_v1_and_v2(self):
        # A v1 and a v2 body built from one session id and one client_request_id derive one command_id.
        v1 = request(session_id=VECTORS['session_id'], client_request_id=SQUEEZE['client_request_id'])
        self.assertEqual(v1['command_id'], SQUEEZE['command_id'])
        raw1, sig1 = signed(v1)
        self.assertEqual(self.bridge.accept(raw1, KEY_ID, sig1)[0], 'new')
        self.assertEqual(self.refused(SQUEEZE['body'].encode(), PALETTE_KEY_ID, SQUEEZE['signature']), (409, 'COMMAND_ID_CONFLICT'))
        self.assertEqual(self.archive.health()['archived'], 1)

    def test_the_v1_route_still_refuses_what_it_refused(self):
        bad = request(mode='issue')
        raw, sig = signed(bad)
        self.assertEqual(self.refused(raw, KEY_ID, sig, self.bridge.accept), (422, 'REQUEST_INVALID'))
        broken = request(issued_at='2026-13-45T25:61:61.000000Z')
        raw, sig = signed(broken)
        self.assertEqual(self.refused(raw, KEY_ID, sig, self.bridge.accept), (422, 'REQUEST_INVALID'))


class PaletteHttpTest(unittest.TestCase):
    """The real handler: one route keyed by the signed subject, 202 only after the PubAck."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = b.Archive(Path(self.temp.name) / 'archive.sqlite')
        self.js = FakeJS()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.bridge = b.Bridge(self.archive, KEYS, self.js, clock=lambda: PALETTE_NOW)
        self.server = b.ThreadingHTTPServer(('127.0.0.1', 0), b.handler(self.bridge, self.loop))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.temp.cleanup()

    def post(self, path, raw, key_id=PALETTE_KEY_ID, signature=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        headers = {'Content-Type': 'application/json'}
        if key_id:
            headers['X-Milkshake-Key-Id'] = key_id
        if signature:
            headers['X-Milkshake-Signature'] = signature
        conn.request('POST', path, body=raw, headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_each_vector_answers_202_and_publishes_on_its_subject(self):
        for number, vector in enumerate(VECTORS['vectors'], start=1):
            status, body = self.post(b.PALETTE_PATH, vector['body'].encode(), signature=vector['signature'])
            self.assertEqual((status, body['success'], body['duplicate'], body['command_id']), (202, True, False, vector['command_id']), vector['name'])
            self.assertEqual(body['received_at'], '2026-11-02T14:05:10.123456Z')
            subject, event, headers = self.js.calls[-1]
            self.assertEqual(subject, vector['subject'])
            self.assertEqual(event['subject'], vector['subject'])
            self.assertEqual(headers, {'Nats-Msg-Id': vector['command_id']})
            self.assertEqual(len(self.js.calls), number)
        self.assertEqual(self.archive.health()['unpublished'], 0)
        status, body = self.post(b.PALETTE_PATH, SQUEEZE['body'].encode(), signature=SQUEEZE['signature'])
        self.assertEqual((status, body['duplicate']), (200, True))
        self.assertEqual(len(self.js.calls), 5)

    def test_a_lone_surrogate_answers_422_on_both_routes_and_the_server_stays_up(self):
        palette = palette_request(command='rule revoke', subject=SUBJECT_OF['rule revoke'], mode='apply', args={'rule_id': 'r42', 'reason': 'a\ud800b'})
        raw, sig = palette_signed(palette)
        status, body = self.post(b.PALETTE_PATH, raw, signature=sig)
        self.assertEqual((status, body['code']), (422, 'REQUEST_INVALID'))
        v1 = request()
        v1['session_id'] = 'sess_\ud800'
        raw1 = b.canonical(v1).encode('utf-8', errors='backslashreplace')
        status, body = self.post(b.PATH, raw1, key_id=KEY_ID, signature='sha256=' + b.sign(KEY, raw1))
        self.assertEqual((status, body['code']), (422, 'REQUEST_INVALID'))
        status, body = self.post(b.PALETTE_PATH, SQUEEZE['body'].encode(), signature=SQUEEZE['signature'])
        self.assertEqual(status, 202)

    def test_bus_down_is_503_and_the_sweep_republishes_on_the_archived_subject(self):
        self.js.fail = ConnectionError()
        vector = BY_NAME['farm-push-plan']
        status, body = self.post(b.PALETTE_PATH, vector['body'].encode(), signature=vector['signature'])
        self.assertEqual((status, body['code']), (503, 'BUS_UNAVAILABLE'))
        self.assertEqual(self.archive.health()['unpublished'], 1)
        self.js.fail = None
        asyncio.run(self.bridge.sweep())
        self.assertEqual(self.archive.health()['unpublished'], 0)
        subject, event, headers = self.js.calls[0]
        self.assertEqual(subject, 'bloodbank.cmd.billing.snapshot.send')
        self.assertEqual(event['type'], 'bloodbank.billing.snapshot.send')
        self.assertEqual(headers, {'Nats-Msg-Id': vector['command_id']})

    def test_other_paths_and_the_wrong_key_purpose(self):
        self.assertEqual(self.post('/v1/command', b'{}', signature='sha256=' + '0' * 64)[0], 404)
        status, body = self.post(b.PATH, SQUEEZE['body'].encode(), signature=SQUEEZE['signature'])
        self.assertEqual((status, body['code']), (401, 'KEY_UNKNOWN'))
        status, body = self.post(b.PALETTE_PATH, b'{}', key_id=KEY_ID, signature='sha256=' + b.sign(KEY, b'{}'))
        self.assertEqual((status, body['code']), (401, 'KEY_UNKNOWN'))


class ResultEventSchemaTest(unittest.TestCase):
    """billing.farm_command.completed|denied|failed: one valid and one invalid example each."""

    DATA = {
        'schema_version': 1, 'command_id': SQUEEZE['command_id'], 'command': 'squeeze',
        'subject': 'bloodbank.cmd.billing.cost.receive', 'client': 'fixture-client', 'mode': 'plan',
        'status': 'planned', 'code': None, 'message': 'planned: nothing written', 'appended': 0,
        'requested_by': 'jarad@automaticai.io', 'claimed_requested_by': None,
        'args_sha256': SQUEEZE['args_sha256'], 'ledger_recorded': True, 'ledger_seq': 12,
        'pushed': True, 'snapshot_sha256': 'a' * 64,
    }

    def event(self, action, **data):
        ids = VECTORS['envelope']['result_event_ids']
        command_id = SQUEEZE['command_id']
        client = data.get('client', 'fixture-client')
        return {
            'specversion': '1.0', 'id': ids[action], 'source': 'urn:33god:service:milkshake',
            'type': f'bloodbank.billing.farm_command.{action}',
            'subject': f'bloodbank.evt.billing.farm_command.{action}',
            'time': '2026-11-02T14:05:12.000000Z', 'datacontenttype': 'application/json',
            'dataschema': f'apicurio://holyfields/bloodbank.billing.farm_command.{action}/versions/1',
            'schemaref': f'bloodbank.billing.farm_command.{action}.v1', 'kind': 'event', 'domain': 'billing',
            'producer': 'milkshake', 'service': 'milkshake-farm-consumer',
            'actor': {'type': 'service', 'agent_id': 'milkshake'},
            'correlationid': command_id, 'causationid': command_id,
            'ordering_key': f'ledger:{client}' if client else 'ledger:global',
            'data': {**self.DATA, **data},
        }

    def test_valid_examples(self):
        b.validate_envelope(self.event('completed'))
        b.validate_envelope(self.event('completed', status='applied', mode='apply', code='FARM_RUN_TIMEOUT',
                                       message='applied, then FARM_RUN_TIMEOUT: 3 event(s) were appended before the run stopped', appended=3))
        b.validate_envelope(self.event('denied', status='refused', code='FARM_COMMAND_NOT_GRANTED', message='no grant', appended=None))
        denied = self.event('denied', status='refused', code='FARM_ACTOR_UNKNOWN', command=None, subject=None, client=None, mode=None,
                            requested_by=None, claimed_requested_by='someone@else.io', args_sha256=None, ledger_recorded=False,
                            ledger_seq=None, pushed=None, snapshot_sha256=None, appended=None, message='not an invoicer')
        b.validate_envelope(denied)
        self.assertEqual(denied['ordering_key'], 'ledger:global')
        b.validate_envelope(self.event('failed', status='failed', code='MERCURY_UNAVAILABLE', message='bank unreachable'))

    def test_invalid_examples(self):
        invalid = [
            self.event('completed', status='refused'),
            self.event('completed', status='failed'),
            self.event('denied', status='planned'),
            self.event('failed', status='refused'),
            self.event('completed', args={'client': 'x'}),  # never args on the bus
            self.event('completed', command='rule add'),
            self.event('completed', code='lowercase_code'),
            self.event('completed', message='x' * 501),
            self.event('completed', appended=-1),
            self.event('completed', pushed='yes'),
        ]
        missing = self.event('completed')
        del missing['data']['appended']
        invalid.append(missing)
        wrong_key = self.event('completed')
        wrong_key['ordering_key'] = 'invoice:fixture-client'
        invalid.append(wrong_key)
        for event in invalid:
            with self.assertRaises(Exception, msg=event['data']):
                b.validate_envelope(event)

    def test_the_result_event_ids_are_the_vectors(self):
        ns = b.NS_FARM_COMMAND
        for action, expected in VECTORS['envelope']['result_event_ids'].items():
            computed = str(b.uuid.uuid5(ns, f"{SQUEEZE['command_id']}:bloodbank.billing.farm_command.{action}"))
            self.assertEqual(computed, expected, action)


class CommandSchemaTest(unittest.TestCase):
    def test_entities_are_registered(self):
        import core.validate as v
        self.assertTrue({'farm_command', 'payment'} <= v.ALLOWED_ENTITIES)
        for typ in ('bloodbank.billing.farm_command.completed', 'bloodbank.billing.payment.receive',
                    'bloodbank.billing.cost.receive', 'bloodbank.billing.cost.close',
                    'bloodbank.billing.rule.cancel', 'bloodbank.billing.snapshot.send'):
            v.assert_type_shape(typ)


if __name__ == '__main__':
    unittest.main()
