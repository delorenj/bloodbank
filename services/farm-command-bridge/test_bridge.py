"""farm-command-bridge: the contract vectors, every refusal, dedupe, PubAck-before-202, and the
Mercury webhook route (Milkshake Story 9.6)."""
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import hmac
import http.client
import importlib.util
import io
import re
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
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


# ---------------------------------------------------------------------------------------------
# Mercury webhooks (Milkshake Story 9.6, docs/farm-commands.md section 16; tests per 16.13).

MERCURY_FIXTURE = Path(__file__).with_name('fixtures') / 'mercury-hook-vectors.json'
MERCURY_FIXTURE_SHA = 'd0617a1dbecf7db672862ce26e3fcbfe805d7336891e1e388c1b40800266213d'
MV = json.loads(MERCURY_FIXTURE.read_text(encoding='utf-8'))
M_SECRETS = tuple((s['endpoint_id'], s['secret']) for s in MV['mercury_secrets'])
M_KEY_ID, M_KEY = MV['key_id'], MV['key']
M_CREATE, M_UPDATE = MV['vectors']
M_ENDPOINT = M_SECRETS[0][0]
M_KEYS = {**KEYS, M_KEY_ID: M_KEY}
# Never in a published byte or a log line: Mercury's amount and counterparty, the secrets, the key.
M_SECRET_TEXT = ('FIXTURE VENDOR INC', '125.5', M_KEY, *(secret for _e, secret in M_SECRETS))


def m_instant(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=UTC)


def m_at(seconds):
    return datetime.fromtimestamp(seconds, UTC)


def m_header(raw, t, secret=M_SECRETS[0][1]):
    return f't={t},v1=' + hmac.new(secret.encode(), str(t).encode() + b'.' + raw, hashlib.sha256).hexdigest()


def m_body(**changes):
    """A compact Mercury body around the create vector's, with keys dropped where a change is ...."""
    value = json.loads(M_CREATE['mercury_body'])
    for key, item in changes.items():
        if item is ...:
            value.pop(key, None)
        else:
            value[key] = item
    return json.dumps(value, separators=(',', ':')).encode()


class MercuryJS:
    """A JetStream that keeps the published bytes; `fail` raises, `delay` stalls the PubAck."""

    def __init__(self):
        self.calls, self.fail, self.delay, self.stream = [], None, 0, 'BLOODBANK_COMMANDS'

    async def publish(self, subject, body, headers=None, timeout=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        self.calls.append((subject, body, headers))
        return SimpleNamespace(stream=self.stream, seq=len(self.calls))


class MercuryFixtureTest(unittest.TestCase):
    def test_the_fixture_is_the_contract_file(self):
        raw = MERCURY_FIXTURE.read_bytes()
        self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (17527, MERCURY_FIXTURE_SHA))
        self.assertEqual(str(b.NS_MERCURY_HOOK), MV['ns_mercury_hook'])
        self.assertEqual(b.NS_MERCURY_HOOK, b.uuid.uuid5(b.uuid.NAMESPACE_URL, 'urn:automaticai:milkshake:mercury-hook'))
        self.assertEqual(MV['window_s'], b.MERCURY_WINDOW_S)

    def test_the_19_signature_cases(self):
        cases = MV['signature_cases']
        self.assertEqual(len(cases), 19)
        bodies = {'create': M_CREATE['mercury_body'].encode(), 'create-tampered': MV['tampered_body'].encode()}
        for case in cases:
            verdict, endpoint_id, matched = b.verify_mercury(bodies[case['body']], case['header'], M_SECRETS, case['now'])
            self.assertEqual((verdict, endpoint_id), (case['verdict'], case['endpoint_id']), case['name'])
            if verdict == 'ok':
                self.assertEqual(matched, endpoint_id, case['name'])
            elif verdict == 'SIGNATURE_EXPIRED':
                self.assertEqual(matched, M_ENDPOINT, case['name'])  # the archive row needs it
            else:
                self.assertIsNone(matched, case['name'])

    def test_each_vector_rebuilds_byte_for_byte(self):
        for vector in MV['vectors']:
            raw = vector['mercury_body'].encode()
            self.assertEqual((len(raw), b.sha256_hex(raw)), (vector['mercury_body_bytes'], vector['mercury_body_sha256']))
            self.assertEqual(b.verify_mercury(raw, vector['header'], M_SECRETS, vector['t'] + 1), ('ok', M_ENDPOINT, M_ENDPOINT))
            event = b.read_mercury_event(raw)
            envelope = b.build_hook(event, M_ENDPOINT, vector['t'], vector['mercury_body_sha256'], vector['received_at'], M_KEY_ID, M_KEY)
            request = b.canonical(envelope['data']['request'])
            self.assertEqual(request, vector['request'], vector['name'])
            self.assertEqual((len(request.encode()), b.sha256_hex(request.encode())), (vector['request_bytes'], vector['request_sha256']))
            self.assertEqual(envelope['data']['signature'], {'alg': 'hmac-sha256', 'key_id': M_KEY_ID, 'value': vector['bridge_signature']})
            self.assertEqual(envelope['data']['body_sha256'], vector['request_sha256'])
            self.assertEqual(envelope['id'], vector['command_id'])
            self.assertEqual(b.hook_command_id(event['event_id']), vector['command_id'])
            encoded = b.canonical(envelope).encode()
            self.assertEqual((len(encoded), b.sha256_hex(encoded)), (vector['envelope_bytes'], vector['envelope_sha256']), vector['name'])
            for action, expected in vector['result_event_ids'].items():
                self.assertEqual(str(b.uuid.uuid5(b.NS_MERCURY_HOOK, f"{vector['command_id']}:bloodbank.billing.transaction.{action}")), expected)

    def test_read_mercury_event_requires_four_fields_only(self):
        event = b.read_mercury_event(m_body(changedPaths=..., resourceVersion=..., occurredAt=..., mergePatch=...))
        self.assertEqual((event['changed_paths'], event['resource_version'], event['occurred_at'], event['created_at'], event['posted_at']),
                         (None, None, None, None, None))
        unreadable = {
            'no id': (m_body(id=...), None),
            'id not a uuid': (m_body(id='evt_1'), None),
            'no resourceId': (m_body(resourceId=...), M_CREATE_EVENT_ID),
            'operation outside the enum': (m_body(operationType='archive'), M_CREATE_EVENT_ID),
            'resourceType empty': (m_body(resourceType=''), M_CREATE_EVENT_ID),
            'resourceType too long': (m_body(resourceType='x' * 65), M_CREATE_EVENT_ID),
            'duplicate keys': (b'{"id":"0b6f3c1e-8a2d-4f5b-9c7e-1d2a3b4c5d6e","id":"0b6f3c1e-8a2d-4f5b-9c7e-1d2a3b4c5d6e"}', None),
            'not json': (b'\xff\xfe', None),
            'an array': (b'[]', None),
        }
        for name, (raw, event_id) in unreadable.items():
            with self.assertRaises(b.EventUnreadable, msg=name) as caught:
                b.read_mercury_event(raw)
            self.assertEqual(caught.exception.event_id, event_id, name)
        upper = b.read_mercury_event(m_body(id=M_CREATE_EVENT_ID.upper(), resourceId=M_CREATE_RESOURCE_ID.upper()))
        self.assertEqual((upper['event_id'], upper['resource_id']), (M_CREATE_EVENT_ID, M_CREATE_RESOURCE_ID))

    def test_optional_fields_become_null_never_a_refusal(self):
        cases = [
            ({'resourceVersion': True}, 'resource_version', None),
            ({'resourceVersion': 0}, 'resource_version', None),
            ({'resourceVersion': '2'}, 'resource_version', None),
            ({'resourceVersion': 7}, 'resource_version', 7),
            ({'occurredAt': '2026-11-02'}, 'occurred_at', None),  # a day, not an instant
            ({'occurredAt': '2026-11-02T14:05:08'}, 'occurred_at', None),  # no offset
            ({'occurredAt': 'yesterday'}, 'occurred_at', None),
            ({'occurredAt': '2026-11-02T10:05:08.5-04:00'}, 'occurred_at', '2026-11-02T14:05:08.500000Z'),
            ({'changedPaths': ['status', 'status', 'amount']}, 'changed_paths', ['amount', 'status']),
            ({'changedPaths': ['status', 'not a path']}, 'changed_paths', None),
            ({'changedPaths': ['x'] * 65}, 'changed_paths', None),
            ({'changedPaths': 'status'}, 'changed_paths', None),
            ({'changedPaths': None}, 'changed_paths', None),
            ({'mergePatch': {'createdAt': '2026-11-01T23:59:59Z', 'postedAt': None}}, 'created_at', '2026-11-01T23:59:59.000000Z'),
            ({'mergePatch': {'postedAt': None}}, 'posted_at', None),
            ({'mergePatch': 'patch'}, 'created_at', None),
        ]
        for changes, field, expected in cases:
            self.assertEqual(b.read_mercury_event(m_body(**changes))[field], expected, changes)

    def test_secrets_and_the_signing_key_from_the_environment(self):
        env = {'MERCURY_WEBHOOK_ENDPOINT_1': M_ENDPOINT.upper(), 'MERCURY_WEBHOOK_SECRET_1': 's1',
               'MERCURY_WEBHOOK_ENDPOINT_2': M_SECRETS[1][0], 'MERCURY_WEBHOOK_SECRET_2': 's2'}
        self.assertEqual(b.mercury_secrets_from_env(env), ((M_ENDPOINT, 's1'), (M_SECRETS[1][0], 's2')))
        self.assertEqual(b.mercury_secrets_from_env({}), ())
        self.assertEqual(b.mercury_secrets_from_env({'MERCURY_WEBHOOK_ENDPOINT_1': '', 'MERCURY_WEBHOOK_SECRET_1': ''}), ())
        for bad in ({'MERCURY_WEBHOOK_SECRET_1': 's1'}, {'MERCURY_WEBHOOK_ENDPOINT_2': M_ENDPOINT},
                    {'MERCURY_WEBHOOK_ENDPOINT_1': 'endpoint', 'MERCURY_WEBHOOK_SECRET_1': 's1'},
                    {'MERCURY_WEBHOOK_ENDPOINT_1': M_ENDPOINT, 'MERCURY_WEBHOOK_SECRET_1': 'op://DeLoSecrets/x/credential'},
                    {**env, 'MERCURY_WEBHOOK_ENDPOINT_2': M_ENDPOINT}):
            with self.assertRaises(ValueError, msg=bad):
                b.mercury_secrets_from_env(bad)
        self.assertEqual(b.hook_key_id({'mercury-hook-2026-11': 'k', 'mercury-hook-2026-12': 'k', 'farm-command-z': 'k'}), 'mercury-hook-2026-12')
        self.assertIsNone(b.hook_key_id(KEYS))


M_CREATE_EVENT_ID = '0b6f3c1e-8a2d-4f5b-9c7e-1d2a3b4c5d6e'
M_CREATE_RESOURCE_ID = '7c9e6679-7425-40de-944b-e07fc1f90ae7'


class MercurySchemaTest(unittest.TestCase):
    """transaction.receive and the three results against the registered schemas."""

    def envelope(self, vector=M_CREATE):
        event = b.read_mercury_event(vector['mercury_body'].encode())
        return b.build_hook(event, M_ENDPOINT, vector['t'], vector['mercury_body_sha256'], vector['received_at'], M_KEY_ID, M_KEY)

    def test_the_vector_envelopes_validate(self):
        for vector in MV['vectors']:
            b.validate_envelope(self.envelope(vector))

    def test_the_request_shape_is_closed(self):
        def broken(change):
            event = self.envelope()
            change(event['data']['request'])
            return event
        invalid = [
            broken(lambda r: r.update(extra=1)),
            broken(lambda r: r.update(amount=-125.5)),
            broken(lambda r: r.update(resource_type='checkingAccount')),
            broken(lambda r: r.update(operation_type='archive')),
            broken(lambda r: r.update(resource_version=0)),
            broken(lambda r: r.update(occurred_at='2026-11-02T14:05:08Z')),
            broken(lambda r: r.update(changed_paths=['status', 'status'])),
            broken(lambda r: r.pop('posted_at')),
            broken(lambda r: r.update(schema_version=2)),
        ]
        wrong_key = self.envelope()
        wrong_key['data']['signature']['key_id'] = 'farm-command-test'
        invalid.append(wrong_key)
        extra_data = self.envelope()
        extra_data['data']['received_at'] = extra_data['time']
        invalid.append(extra_data)
        bad_key = self.envelope()
        bad_key['idempotency_key'] = 'billing.transaction.receive:client:x:command:' + bad_key['id']
        invalid.append(bad_key)
        for event in invalid:
            with self.assertRaises(Exception, msg=event['data']):
                b.validate_envelope(event)
        nulls = self.envelope()
        nulls['data']['request'].update(occurred_at=None, resource_version=None, changed_paths=None)
        b.validate_envelope(nulls)

    RESULT = {
        'schema_version': 1, 'command_id': M_CREATE['command_id'], 'event_id': M_CREATE_EVENT_ID,
        'endpoint_id': M_ENDPOINT, 'resource_id': M_CREATE_RESOURCE_ID, 'operation_type': 'create',
        'status': 'synced', 'code': None, 'message': 'synced', 'batch_id': MV['batch']['batch_id'],
        'since': '2026-10-30', 'read_at': '2026-11-02T14:06:46.600000Z', 'batch_size': 2, 'new': 1,
        'changed': 0, 'unchanged': 40, 'tagged': 1, 'pushed': True, 'pushed_clients': ['fixture-client'],
    }

    def result(self, action, **data):
        command_id = M_CREATE['command_id']
        return {
            'specversion': '1.0', 'id': M_CREATE['result_event_ids'][action], 'source': 'urn:33god:service:milkshake',
            'type': f'bloodbank.billing.transaction.{action}', 'subject': f'bloodbank.evt.billing.transaction.{action}',
            'time': '2026-11-02T14:06:50.000000Z', 'datacontenttype': 'application/json',
            'dataschema': f'apicurio://holyfields/bloodbank.billing.transaction.{action}/versions/1',
            'schemaref': f'bloodbank.billing.transaction.{action}.v1', 'kind': 'event', 'domain': 'billing',
            'producer': 'milkshake', 'service': 'milkshake-farm-consumer',
            'actor': {'type': 'service', 'agent_id': 'milkshake'},
            'correlationid': command_id, 'causationid': command_id, 'ordering_key': 'ledger:global',
            'data': {**self.RESULT, **data},
        }

    def test_valid_results(self):
        none = dict(new=None, changed=None, unchanged=None, tagged=None, pushed=None, pushed_clients=None)
        b.validate_envelope(self.result('recorded'))
        b.validate_envelope(self.result('recorded', code='FARM_PUSH_FAILED', message='synced, then FARM_PUSH_FAILED', pushed=False))
        b.validate_envelope(self.result('recorded', since=None))  # a full read
        b.validate_envelope(self.result('recorded', status='covered', batch_size=None, **none))
        b.validate_envelope(self.result('denied', status='refused', code='MERCURY_HOOK_ENDPOINT_UNKNOWN', message='endpoint not registered',
                                        batch_id=None, since=None, read_at=None, batch_size=None, **none))
        b.validate_envelope(self.result('denied', status='refused', code='MERCURY_HOOK_INVALID', message='unreadable',
                                        command_id=None, event_id=None, endpoint_id=None, resource_id=None, operation_type=None,
                                        batch_id=None, since=None, read_at=None, batch_size=None, **none))
        b.validate_envelope(self.result('failed', status='failed', code='MERCURY_UNAVAILABLE', message='bank unreachable', **none))

    def test_invalid_results(self):
        invalid = [
            self.result('recorded', status='refused'),
            self.result('recorded', status='failed'),
            self.result('recorded', status='ignored'),  # the bridge's ignored never reaches the bus
            self.result('denied', status='synced'),
            self.result('failed', status='covered'),
            self.result('recorded', amount=-125.5),
            self.result('recorded', code='lowercase'),
            self.result('recorded', message='x' * 501),
            self.result('recorded', new=-1),
            self.result('recorded', since='2026-10-30T00:00:00Z'),
            self.result('recorded', read_at='2026-11-02T14:06:46Z'),
            self.result('recorded', pushed_clients=['Not A Slug']),
            self.result('recorded', pushed='yes'),
        ]
        missing = self.result('recorded')
        del missing['data']['tagged']
        invalid.append(missing)
        client_bucket = self.result('recorded')
        client_bucket['ordering_key'] = 'ledger:fixture-client'
        invalid.append(client_bucket)
        for event in invalid:
            with self.assertRaises(Exception, msg=event['data']):
                b.validate_envelope(event)

    def test_types_are_registered(self):
        import core.validate as v
        for typ in ('bloodbank.billing.transaction.receive', 'bloodbank.billing.transaction.recorded',
                    'bloodbank.billing.transaction.denied', 'bloodbank.billing.transaction.failed'):
            v.assert_type_shape(typ)


class MercuryHttpTest(unittest.TestCase):
    """The real handler over a socket, with a fake JetStream and a settable clock (16.13)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / 'archive.sqlite'
        self.archive = b.Archive(path)
        self.mercury = b.MercuryArchive(path)
        self.js = MercuryJS()
        self.now = m_instant(M_CREATE['received_at'])
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.bridge = b.Bridge(self.archive, M_KEYS, self.js, clock=lambda: self.now,
                               mercury_archive=self.mercury, mercury_secrets=M_SECRETS)
        self.server = b.ThreadingHTTPServer(('127.0.0.1', 0), b.handler(self.bridge, self.loop))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.logs = io.StringIO()
        self._stdout = contextlib.redirect_stdout(self.logs)
        self._stdout.__enter__()
        b.validate_envelope(MercurySchemaTest().envelope())  # warm the schema registry, as a running bridge is

    def tearDown(self):
        self._stdout.__exit__(None, None, None)
        self.server.shutdown()
        self.server.server_close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.temp.cleanup()
        logs = self.logs.getvalue()
        for text in (*M_SECRET_TEXT, M_CREATE['signature'], M_UPDATE['signature'], M_CREATE['bridge_signature'],
                     M_CREATE['mercury_body'], 'mergePatch'):
            self.assertNotIn(text, logs)  # logs name ids, status and code only

    # -- transport helpers ----------------------------------------------------------------

    def post(self, raw, header=None, host=b.MERCURY_HOST, path=b.MERCURY_PATH, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        sent = {'Host': host, **(headers or {})}
        if header is not None:
            sent['Mercury-Signature'] = header
        conn.request('POST', path, body=raw, headers=sent)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def raw_http(self, data, then=None):
        """Write raw request bytes (optionally more after a pause), read until the server closes."""
        sock = socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=10)
        try:
            sock.sendall(data)
            if then is not None:
                then(sock)
            chunks = []
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                chunks.append(part)
        finally:
            sock.close()
        head, _, body = b''.join(chunks).partition(b'\r\n\r\n')
        return int(head.split(b' ')[1]), json.loads(body)

    def head(self, header=None, *lines):
        out = [f'POST {b.MERCURY_PATH} HTTP/1.1', f'Host: {b.MERCURY_HOST}']
        if header:
            out.append(f'Mercury-Signature: {header}')
        out.extend(lines)
        return ('\r\n'.join(out) + '\r\n\r\n').encode()

    def chunked(self, raw, header, sizes=(100, 7, 1000)):
        parts, at = [], 0
        for size in (*sizes, len(raw)):
            piece = raw[at:at + size]
            if piece:
                parts.append(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
            at += len(piece)
        return self.head(header, 'Transfer-Encoding: chunked') + b''.join(parts) + b'0\r\n\r\n'

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        conn.request('GET', path)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def rows(self, sql):
        with sqlite3.connect(self.mercury.path) as db:
            return db.execute(sql).fetchall()

    def send(self, vector, now=None):
        self.now = now or m_instant(vector['received_at'])
        return self.post(vector['mercury_body'].encode(), vector['header'])

    # -- the vectors ----------------------------------------------------------------------

    def test_each_vector_publishes_the_golden_envelope(self):
        for number, vector in enumerate(MV['vectors'], start=1):
            status, body = self.send(vector)
            self.assertEqual((status, body), (202, {'success': True, 'duplicate': False, 'command_id': vector['command_id'], 'published': True}))
            subject, published, headers = self.js.calls[-1]
            self.assertEqual(len(self.js.calls), number)
            self.assertEqual(subject, 'bloodbank.cmd.billing.transaction.receive')
            self.assertEqual(headers, {'Nats-Msg-Id': vector['command_id']})
            self.assertEqual((len(published), b.sha256_hex(published)), (vector['envelope_bytes'], vector['envelope_sha256']))
            envelope = json.loads(published)
            self.assertEqual(b.canonical(envelope['data']['request']), vector['request'])
            self.assertEqual(envelope['data']['signature']['value'], vector['bridge_signature'])
            b.validate_envelope(envelope)
            for text in M_SECRET_TEXT + ('bankDescription":', 'previousValues', 'mergePatch'):
                self.assertNotIn(text.encode(), published, text)
            (archived_request,), = self.rows(f"SELECT envelope FROM mercury_events WHERE event_id='{envelope['data']['request']['event_id']}'")
            self.assertEqual(b.canonical(json.loads(archived_request)['data']['request']), vector['request'])
            (body_blob, sha, t), = self.rows(f"SELECT body, body_sha256, mercury_t FROM mercury_events WHERE command_id='{vector['command_id']}'")
            self.assertEqual((bytes(body_blob), sha, t), (vector['mercury_body'].encode(), vector['mercury_body_sha256'], vector['t']))
        self.assertEqual(self.rows("SELECT state FROM mercury_delivery ORDER BY rowid"), [('published',), ('published',)])
        status, health = self.get('/readyz')
        self.assertEqual((status, health['mercury_archived'], health['mercury_unpublished'], health['mercury_alarm']), (200, 2, 0, None))
        self.assertEqual(health['mercury_last_received_at'], M_UPDATE['received_at'])

    def test_a_chunked_request_gives_the_same_bytes(self):
        for vector in MV['vectors']:
            self.now = m_instant(vector['received_at'])
            status, body = self.raw_http(self.chunked(vector['mercury_body'].encode(), vector['header']))
            self.assertEqual((status, body['published']), (202, True), vector['name'])
            _subject, published, _headers = self.js.calls[-1]
            self.assertEqual(b.sha256_hex(published), vector['envelope_sha256'])
        # chunk extensions and a trailer are framing, not body
        self.mercury_reset()
        self.now = m_instant(M_CREATE['received_at'])
        raw = M_CREATE['mercury_body'].encode()
        data = self.head(M_CREATE['header'], 'Transfer-Encoding: Chunked') + f'{len(raw):x};ext=1\r\n'.encode() + raw + b'\r\n0\r\nX-Trailer: 1\r\n\r\n'
        self.assertEqual(self.raw_http(data)[0], 202)
        self.assertEqual(b.sha256_hex(self.js.calls[-1][1]), M_CREATE['envelope_sha256'])

    def mercury_reset(self):
        self.temp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp2.cleanup)
        self.mercury = self.bridge.mercury_archive = b.MercuryArchive(Path(self.temp2.name) / 'archive.sqlite')

    def test_the_raw_body_is_what_is_signed(self):
        reserialized = json.dumps(json.loads(M_CREATE['mercury_body'])).encode()
        self.assertEqual(self.post(reserialized, M_CREATE['header']), (401, {'success': False, 'error': 'SIGNATURE_INVALID', 'code': 'SIGNATURE_INVALID'}))
        compacted = json.dumps(json.loads(M_UPDATE['mercury_body']), separators=(',', ':')).encode()
        self.now = m_instant(M_UPDATE['received_at'])
        self.assertEqual(self.post(compacted, M_UPDATE['header'])[0], 401)
        self.assertEqual(self.post(M_CREATE['mercury_body'].encode(), None)[1]['code'], 'SIGNATURE_MISSING')
        self.assertEqual(self.post(MV['tampered_body'].encode(), M_CREATE['header'])[1]['code'], 'SIGNATURE_INVALID')
        self.assertEqual((self.js.calls, self.rows('SELECT count(*) FROM mercury_ignored')), ([], [(0,)]))

    # -- the window and dedupe ------------------------------------------------------------

    def test_a_stale_t_is_archived_ignored_with_200_and_a_later_attempt_publishes(self):
        status, body = self.send(M_CREATE, now=m_at(M_CREATE['t'] + 301))
        self.assertEqual((status, body), (200, {'success': True, 'ignored': True, 'code': 'SIGNATURE_EXPIRED'}))
        self.assertEqual(self.js.calls, [])
        self.assertEqual(self.rows('SELECT event_id, endpoint_id, code, mercury_t FROM mercury_ignored'),
                         [(M_CREATE_EVENT_ID, M_ENDPOINT, 'SIGNATURE_EXPIRED', M_CREATE['t'])])
        status, health = self.get('/readyz')
        self.assertEqual((status, health['ready'], health['mercury_alarm']['code']), (503, False, 'SIGNATURE_EXPIRED'))
        self.assertEqual(health['mercury_alarm']['event_id'], M_CREATE_EVENT_ID)
        self.assertEqual(self.get('/healthz')[0], 200)  # liveness stays up
        status, body = self.send(M_CREATE)  # in the window: no mercury_events row blocks it
        self.assertEqual((status, body['published']), (202, True))
        self.assertEqual(b.sha256_hex(self.js.calls[0][1]), M_CREATE['envelope_sha256'])
        self.now = m_at(M_CREATE['t'] + 302) + timedelta(hours=24)  # the alarm lasts 24 hours
        status, health = self.get('/readyz')
        self.assertEqual((status, health['mercury_alarm']), (200, None))
        self.assertEqual(health['mercury_ignored']['SIGNATURE_EXPIRED'], 1)

    def test_a_duplicate_an_hour_later_is_200_with_one_command_never_401(self):
        self.assertEqual(self.send(M_CREATE)[0], 202)
        status, body = self.send(M_CREATE, now=m_at(M_CREATE['t'] + 3600))
        self.assertEqual((status, body), (200, {'success': True, 'duplicate': True, 'command_id': M_CREATE['command_id']}))
        self.assertEqual(len(self.js.calls), 1)
        self.assertEqual(self.rows('SELECT count(*) FROM mercury_events'), [(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM mercury_ignored'), [(0,)])
        status, body = self.send(M_CREATE, now=m_at(M_CREATE['t'] - 3600))  # ahead too
        self.assertEqual((status, body['duplicate']), (200, True))

    def test_same_event_twice_pending_published_and_conflict(self):
        self.js.fail = ConnectionError()
        status, body = self.send(M_CREATE)
        self.assertEqual((status, body['published']), (202, False))
        self.assertEqual(self.rows('SELECT state, error FROM mercury_delivery'), [('pending', 'publish:ConnectionError')])
        self.js.fail = None
        status, body = self.send(M_CREATE)  # a pending row is published by the duplicate
        self.assertEqual((status, body), (200, {'success': True, 'duplicate': True, 'command_id': M_CREATE['command_id']}))
        self.assertEqual(len(self.js.calls), 1)
        self.assertEqual(b.sha256_hex(self.js.calls[0][1]), M_CREATE['envelope_sha256'])  # the stored envelope, as is
        self.assertEqual(self.rows('SELECT state, error FROM mercury_delivery'), [('published', '')])
        self.assertEqual(self.send(M_CREATE)[0], 200)  # a published row is not re-sent
        self.assertEqual(len(self.js.calls), 1)
        tampered = MV['tampered_body'].encode()
        status, body = self.post(tampered, m_header(tampered, M_CREATE['t']))
        self.assertEqual((status, body), (200, {'success': True, 'duplicate': True, 'conflict': True}))
        self.assertEqual(len(self.js.calls), 1)
        self.assertEqual(self.rows('SELECT event_id, body_sha256 FROM mercury_conflicts'), [(M_CREATE_EVENT_ID, b.sha256_hex(tampered))])
        self.assertIn('EVENT_ID_CONFLICT', self.logs.getvalue())
        self.assertEqual(self.rows('SELECT count(*) FROM mercury_events'), [(1,)])
        with sqlite3.connect(self.mercury.path) as db:
            for sql in ("UPDATE mercury_events SET body_sha256='x'", 'DELETE FROM mercury_events',
                        "UPDATE mercury_conflicts SET body_sha256='x'", "UPDATE mercury_delivery SET state='pending'",
                        'DELETE FROM mercury_delivery'):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    db.execute(sql)

    # -- authentic but not acted on -------------------------------------------------------

    def test_authentic_unreadable_bodies_are_ignored_with_200_and_alarm(self):
        for raw, event_id in ((m_body(id=...), None), (m_body(operationType='archive'), M_CREATE_EVENT_ID), (b'not json', None)):
            status, body = self.post(raw, m_header(raw, M_CREATE['t']))
            self.assertEqual((status, body), (200, {'success': True, 'ignored': True, 'code': 'EVENT_UNREADABLE'}))
            self.assertEqual(self.rows('SELECT event_id, code FROM mercury_ignored ORDER BY rowid DESC LIMIT 1'), [(event_id, 'EVENT_UNREADABLE')])
        self.assertEqual(self.js.calls, [])
        status, health = self.get('/readyz')
        self.assertEqual((status, health['mercury_alarm']['code'], health['mercury_ignored']['EVENT_UNREADABLE']), (503, 'EVENT_UNREADABLE', 3))

    def test_a_balance_event_is_ignored_without_an_alarm(self):
        raw = m_body(resourceType='checkingAccount')
        status, body = self.post(raw, m_header(raw, M_CREATE['t']))
        self.assertEqual((status, body), (200, {'success': True, 'ignored': True, 'code': 'RESOURCE_IGNORED'}))
        self.assertEqual(self.js.calls, [])
        status, health = self.get('/readyz')
        self.assertEqual((status, health['mercury_alarm'], health['mercury_ignored']['RESOURCE_IGNORED']), (200, None, 1))

    def test_a_create_without_changed_paths_version_or_occurred_at_is_published(self):
        raw = m_body(changedPaths=..., resourceVersion=..., occurredAt=...)
        status, body = self.post(raw, m_header(raw, M_CREATE['t']))
        self.assertEqual((status, body['published']), (202, True))
        envelope = json.loads(self.js.calls[0][1])
        request = envelope['data']['request']
        self.assertEqual((request['changed_paths'], request['resource_version'], request['occurred_at']), (None, None, None))
        self.assertEqual(request['received_at'], M_CREATE['received_at'])
        b.validate_envelope(envelope)

    def test_an_envelope_that_fails_its_schema_is_ignored_never_4xx(self):
        from unittest.mock import patch
        with patch.object(b, 'validate_envelope', side_effect=ValueError('bridge bug')):
            status, body = self.send(M_CREATE)
        self.assertEqual((status, body), (200, {'success': True, 'ignored': True, 'code': 'ENVELOPE_INVALID'}))
        self.assertEqual((self.js.calls, self.rows('SELECT count(*) FROM mercury_events')), ([], [(0,)]))
        self.assertEqual(self.get('/readyz')[1]['mercury_alarm']['code'], 'ENVELOPE_INVALID')
        self.assertEqual(self.send(M_CREATE)[0], 202)  # an ignored event never blocks a later attempt

    # -- NATS, the archive, configuration -------------------------------------------------

    def test_nats_down_is_202_unpublished_and_the_sweep_publishes(self):
        self.js.fail = ConnectionError()
        status, body = self.send(M_CREATE)
        self.assertEqual((status, body['published']), (202, False))
        status, health = self.get('/readyz')
        self.assertEqual((status, health['mercury_unpublished']), (503, 1))
        self.js.fail = None
        self.js.delay = 5  # a PubAck that never comes in time
        started = time.monotonic()
        status, body = self.send(M_UPDATE)
        self.assertEqual((status, body['published']), (202, False))
        self.assertLess(time.monotonic() - started, 3.0)  # inside Mercury's 5 s
        self.assertEqual(self.rows("SELECT error FROM mercury_delivery WHERE event_id='1c7f4d2f-9b3e-4a6c-8d8f-2e3b4c5d6e7f'"), [('publish:TimeoutError',)])
        self.js.delay = 0
        asyncio.run_coroutine_threadsafe(self.bridge.sweep(), self.loop).result(timeout=10)
        self.assertEqual(sorted(b.sha256_hex(c[1]) for c in self.js.calls), sorted([M_CREATE['envelope_sha256'], M_UPDATE['envelope_sha256']]))
        status, health = self.get('/readyz')
        self.assertEqual((status, health['mercury_unpublished']), (200, 0))
        self.assertIn('republished', self.logs.getvalue())

    def test_a_locked_archive_answers_503_within_2_s(self):
        blocker = sqlite3.connect(self.mercury.path, timeout=0)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            started = time.monotonic()
            status, body = self.send(M_CREATE)
            elapsed = time.monotonic() - started
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual((status, body['code']), (503, 'ARCHIVE_UNAVAILABLE'))
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(elapsed, 1.0)  # it waited for the lock, then gave up
        self.assertEqual(self.js.calls, [])
        self.assertEqual(self.send(M_CREATE)[0], 202)  # Mercury's retry then lands

    def test_unconfigured_is_503_and_the_farm_routes_still_work(self):
        for bridge in (b.Bridge(self.archive, M_KEYS, self.js, clock=lambda: PALETTE_NOW, mercury_archive=self.mercury),
                       b.Bridge(self.archive, KEYS, self.js, clock=lambda: PALETTE_NOW, mercury_archive=self.mercury, mercury_secrets=M_SECRETS)):
            self.server.RequestHandlerClass = b.handler(bridge, self.loop)
            status, body = self.send(M_CREATE)
            self.assertEqual((status, body['code']), (503, 'HOOK_UNCONFIGURED'))
            self.assertFalse(bridge.health()['mercury_configured'])
        status, body = self.post(SQUEEZE['body'].encode(), host='farm-commands.delo.sh', path=b.PALETTE_PATH,
                                 headers={'Content-Type': 'application/json', 'X-Milkshake-Key-Id': PALETTE_KEY_ID, 'X-Milkshake-Signature': SQUEEZE['signature']})
        self.assertEqual((status, body['command_id']), (202, SQUEEZE['command_id']))

    def test_the_host_check_both_ways(self):
        self.assertEqual(self.post(M_CREATE['mercury_body'].encode(), M_CREATE['header'], host='farm-commands.delo.sh')[0], 404)
        self.assertEqual(self.post(M_CREATE['mercury_body'].encode(), M_CREATE['header'], host='127.0.0.1')[0], 404)
        for path, key_id, vector in ((b.PALETTE_PATH, PALETTE_KEY_ID, SQUEEZE),):
            status, _ = self.post(vector['body'].encode(), host=b.MERCURY_HOST, path=path,
                                  headers={'Content-Type': 'application/json', 'X-Milkshake-Key-Id': key_id, 'X-Milkshake-Signature': vector['signature']})
            self.assertEqual(status, 404)
        raw, sig = signed(request())
        status, _ = self.post(raw, host='MERCURY-HOOKS.delo.sh:443', path=b.PATH,
                              headers={'Content-Type': 'application/json', 'X-Milkshake-Key-Id': KEY_ID, 'X-Milkshake-Signature': sig})
        self.assertEqual(status, 404)
        self.assertEqual(self.js.calls, [])
        self.assertEqual(self.send(M_CREATE, now=None)[0], 202)  # the Mercury host, any port spelling
        self.now = m_instant(M_UPDATE['received_at'])
        self.assertEqual(self.post(M_UPDATE['mercury_body'].encode(), M_UPDATE['header'], host='Mercury-Hooks.delo.sh:443')[0], 202)

    def test_body_limits_and_unreadable_framing(self):
        header = M_CREATE['header']
        self.assertEqual(self.raw_http(self.head(header, f'Content-Length: {b.MERCURY_MAX_BODY + 1}')),
                         (413, {'success': False, 'error': 'BODY_TOO_LARGE', 'code': 'BODY_TOO_LARGE'}))
        over = self.head(header, 'Transfer-Encoding: chunked') + b'10000\r\n' + b'x' * 0x10000 + b'\r\n1\r\n'
        self.assertEqual(self.raw_http(over)[1]['code'], 'BODY_TOO_LARGE')
        for lines, extra in ((('Content-Length: 0',), b''), (('Content-Length: -5',), b''), (('Content-Length: ten',), b''),
                             ((), b''),  # no length and no chunking
                             (('Transfer-Encoding: chunked',), b'zz\r\nabc\r\n0\r\n\r\n'),  # broken framing
                             (('Transfer-Encoding: chunked',), b'5\r\nabcdefgh\r\n0\r\n\r\n'),  # chunk longer than said
                             (('Transfer-Encoding: chunked',), b'0\r\n\r\n'),  # an empty body
                             (('Transfer-Encoding: gzip',), b'')):
            status, body = self.raw_http(self.head(header, *lines) + extra)
            self.assertEqual((status, body['code']), (503, 'BODY_UNREADABLE'), lines)
        from unittest.mock import patch
        with patch.object(b, 'MERCURY_READ_TIMEOUT_S', 0.5):
            started = time.monotonic()
            status, body = self.raw_http(self.head(header, 'Content-Length: 378') + b'{"id":')  # then stalls
            self.assertEqual((status, body['code']), (503, 'BODY_UNREADABLE'))
            self.assertLess(time.monotonic() - started, 3.0)
        exactly = m_body(note='y' * (b.MERCURY_MAX_BODY - len(m_body()) - len(',"note":""')))
        self.assertEqual(len(exactly), b.MERCURY_MAX_BODY)
        self.assertEqual(self.post(exactly, m_header(exactly, M_CREATE['t']))[0], 202)  # 65536 bytes is allowed
        self.assertEqual(self.js.calls[0][0], b.MERCURY_SUBJECT)

    def test_republish_reads_the_archived_envelope_by_event_id(self):
        self.assertEqual(self.send(M_CREATE)[0], 202)
        stored = self.mercury.get(M_CREATE_EVENT_ID)
        self.assertEqual(b.sha256_hex(b.canonical(stored).encode()), M_CREATE['envelope_sha256'])
        self.assertIsNone(self.mercury.get('00000000-0000-4000-8000-000000000000'))
        asyncio.run_coroutine_threadsafe(self.bridge.publish_mercury(stored), self.loop).result(timeout=10)
        self.assertEqual([b.sha256_hex(c[1]) for c in self.js.calls], [M_CREATE['envelope_sha256']] * 2)


if __name__ == '__main__':
    unittest.main()
