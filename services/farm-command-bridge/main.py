"""Narrow HTTPS ingress for the Farm's signed commands (Milkshake Stories 4.5 and 10.3).

The portal Worker POSTs a signed Create Invoice request to /v1/invoice-commands, and a signed
command-palette request (v2) to /v1/commands, which routes by the signed `subject`. This bridge checks the
HMAC-SHA256 over the raw bytes before it parses anything, then the canonical form, the request
fields, the five-minute window, archives the request immutably (SQLite, synchronous=FULL), wraps
it in a bloodbank.billing.invoice.generate command envelope and answers 202 only after the
JetStream PubAck on BLOODBANK_COMMANDS. Milkshake's milkshake-farm-consumer re-verifies the
signature and decides on its own ledger who may ask for what; this bridge authorizes nothing.

A second trust source shares the process (Milkshake Story 9.6): Mercury POSTs its transaction
webhooks to /v1/mercury-webhooks on its own host, mercury-hooks.delo.sh. The bridge verifies
Mercury-Signature over the raw bytes with each configured endpoint secret, archives the raw body as
evidence, and publishes a minimal request (ids, types and instants, never an amount or a name)
signed with its own mercury-hook-* key as bloodbank.billing.transaction.receive. Once a body is
authentic it is never answered 4xx: Mercury does not retry a 4xx and counts it toward disabling
the endpoint.

Contract: Milkshake docs/farm-commands.md sections 2, 4 and 5 (Create Invoice), 14.3 to 14.5
(the palette) and 16.3 to 16.5 (Mercury webhooks). Precedent: services/cost-bridge.
Logs name command_id, event_id, endpoint_id, status and code only: never a body, a header, a
reason, a key, a secret or a signature.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-hooks'))
from core.validate import validate_envelope  # noqa: E402

TYPE = 'bloodbank.billing.invoice.generate'
SUBJECT = 'bloodbank.cmd.billing.invoice.generate'
STREAM = 'BLOODBANK_COMMANDS'
PATH = '/v1/invoice-commands'
PALETTE_PATH = '/v1/commands'
# Key purpose (contract 14.1 P4): a key id signs one route's bodies and nothing else.
INVOICE_KEY_PREFIX = 'farm-invoice-'
PALETTE_KEY_PREFIX = 'farm-command-'
# 200 (the schemas' data.actor cap) - len('farm:' + '/' + command_id) = 200 - 42 (contract 14.3.1).
PALETTE_MAX_EMAIL = 158
# The bridge authorizes nothing, it routes: the signed subject picks the command it must name.
ROUTES = {
    'bloodbank.cmd.billing.cost.receive': 'squeeze',
    'bloodbank.cmd.billing.cost.close': 'boi review',
    'bloodbank.cmd.billing.rule.cancel': 'rule revoke',
    'bloodbank.cmd.billing.payment.receive': 'yrd payment',
    'bloodbank.cmd.billing.snapshot.send': 'farm push',
}
CLIENT_COMMANDS = frozenset({'squeeze', 'yrd payment', 'farm push'})  # the entries that take `client`
SPAN_COMMANDS = frozenset({'squeeze'})  # the span rule (14.3.2 rule 2)
MAX_BODY = 4096
WINDOW = timedelta(seconds=300)
MIN_KEY_LENGTH = 32
NS_FARM_COMMAND = uuid.UUID('50c023df-51df-574c-be02-94deb1cbdfd5')
FIELDS = ('schema_version', 'command_id', 'client_request_id', 'client', 'period', 'date_from',
          'date_to', 'mode', 'accept_incomplete_reason', 'approved_by', 'requested_by',
          'session_id', 'issued_at')
PALETTE_FIELDS = ('schema_version', 'command_id', 'client_request_id', 'subject', 'command', 'client',
                  'args', 'mode', 'requested_by', 'session_id', 'issued_at')
SIGNATURE = re.compile(r'sha256=([0-9a-f]{64})\Z')
INSTANT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
SLUG = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*\Z')
DAY = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])\Z')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')  # rule 4: C0 and DEL
LONE_SURROGATE = re.compile(r'[\ud800-\udfff]')  # rule 5: a str from json.loads holds a half only if it is lone

# Mercury webhooks (contract section 16).
MERCURY_HOST = 'mercury-hooks.delo.sh'
MERCURY_PATH = '/v1/mercury-webhooks'
MERCURY_TYPE = 'bloodbank.billing.transaction.receive'
MERCURY_SUBJECT = 'bloodbank.cmd.billing.transaction.receive'
MERCURY_KEY_PREFIX = 'mercury-hook-'
MERCURY_MAX_BODY = 65536
MERCURY_WINDOW_S = 300
MERCURY_READ_TIMEOUT_S = 3  # a body read that stalls this long is 503 BODY_UNREADABLE
MERCURY_BUSY_TIMEOUT_S = 1.5  # the archive lock surfaces inside Mercury's 5 s, as a 503
MERCURY_PUBACK_TIMEOUT_S = 2
MERCURY_ALARM_WINDOW = timedelta(hours=24)
MERCURY_ALARM_CODES = ('EVENT_UNREADABLE', 'ENVELOPE_INVALID', 'SIGNATURE_EXPIRED')
MERCURY_IGNORED_CODES = ('EVENT_UNREADABLE', 'SIGNATURE_EXPIRED', 'RESOURCE_IGNORED', 'ENVELOPE_INVALID')
MERCURY_ENDPOINT_SLOTS = ('1', '2')
NS_MERCURY_HOOK = uuid.UUID('f4a8a3aa-c580-5dc5-bcb4-ac5e108bc3d0')  # uuid5(NAMESPACE_URL, "urn:automaticai:milkshake:mercury-hook")
MERCURY_T = re.compile(r'[0-9]{1,12}\Z')
MERCURY_V1 = re.compile(r'[0-9a-f]{64}\Z')
MERCURY_PATH_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,63}\Z')
MERCURY_OPERATIONS = ('create', 'update', 'delete')
MERCURY_MAX_PATHS = 64
CHUNK_SIZE = re.compile(rb'[0-9A-Fa-f]{1,8}\Z')
MAX_CHUNK_LINE = 1024
MAX_TRAILERS = 64


class Refusal(Exception):
    """An HTTP refusal with a stable code (contract section 10)."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def canonical(value):
    """Milkshake's canonical(): sorted keys (code-point order), no whitespace, raw UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def sign(key, body):
    return hmac.new(key.encode('utf-8'), body, hashlib.sha256).hexdigest()


def command_id_for(session_id, client_request_id):
    return str(uuid.uuid5(NS_FARM_COMMAND, f'{session_id}:{client_request_id}'))


def stamp(moment):
    return moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def keys_from_env(text):
    """FARM_COMMAND_KEYS: {"<key_id>": "<key>", ...}; the current key and, mid-rotation, the old."""
    keys = json.loads(text)
    if not isinstance(keys, dict) or not keys:
        raise ValueError('FARM_COMMAND_KEYS holds no keys')
    for key_id, key in keys.items():
        if not isinstance(key, str) or len(key) < MIN_KEY_LENGTH or not key_id:
            raise ValueError(f'FARM_COMMAND_KEYS: key {key_id!r} is unusable')
    return keys


def verify(raw, key_id, signature_header, keys, prefix=INVOICE_KEY_PREFIX):
    """Step 2, in this order: the signature header's shape (SIGNATURE_MISSING), a key id of this
    route's purpose that is held (KEY_UNKNOWN), the HMAC over the raw bytes (SIGNATURE_INVALID).
    Nothing is parsed before it."""
    match = SIGNATURE.match(signature_header or '')
    if not match:
        raise Refusal(401, 'SIGNATURE_MISSING')
    key = keys.get(key_id or '') if (key_id or '').startswith(prefix) else None
    if key is None:
        raise Refusal(401, 'KEY_UNKNOWN')
    if not hmac.compare_digest(sign(key, raw), match.group(1)):
        raise Refusal(401, 'SIGNATURE_INVALID')
    return match.group(1)


def _no_duplicates(pairs):
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError('duplicate key')
        seen[name] = value
    return seen


def parse(raw):
    """Step 3: UTF-8 JSON whose canonical form is exactly the bytes that were signed.

    JSON can spell a lone surrogate ("\\ud800"); json.loads yields a str that cannot be encoded to
    UTF-8. Re-encoding the canonical text therefore sits inside the try: a lone surrogate is
    422 REQUEST_INVALID (contract rule 5), never an exception that drops the connection.
    """
    try:
        request = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal(400, 'BODY_INVALID') from None
    if not isinstance(request, dict):
        raise Refusal(400, 'BODY_INVALID')
    try:
        text = canonical(request).encode('utf-8')
    except (UnicodeEncodeError, ValueError, RecursionError):
        raise Refusal(422, 'REQUEST_INVALID') from None
    if text != raw:
        raise Refusal(422, 'BODY_NOT_CANONICAL')
    return request


def check_request(request):
    """Step 4: the rules of contract section 2.1 the JSON Schema cannot say (the schema runs
    on the envelope at step 7 and covers each field's type and pattern)."""
    def bad():
        return Refusal(422, 'REQUEST_INVALID')
    if set(request) != set(FIELDS) or request['schema_version'] != 1 or isinstance(request['schema_version'], bool):
        raise bad()
    text = ('command_id', 'client_request_id', 'client', 'mode', 'requested_by', 'session_id', 'issued_at')
    if not all(isinstance(request[f], str) for f in text):
        raise bad()
    if request['command_id'] != command_id_for(request['session_id'], request['client_request_id']):
        raise bad()
    if request['mode'] not in ('plan', 'apply') or not INSTANT.match(request['issued_at']):
        raise bad()
    period, start, end = request['period'], request['date_from'], request['date_to']
    try:
        if period is not None:
            if start is not None or end is not None:
                raise bad()
        elif start is None or end is None or date.fromisoformat(end) < date.fromisoformat(start):
            raise bad()
    except (TypeError, ValueError):
        raise bad() from None
    reason, approver = request['accept_incomplete_reason'], request['approved_by']
    if reason is not None:
        if request['mode'] != 'apply' or approver != request['requested_by'] or not isinstance(reason, str) or reason != reason.strip():
            raise bad()
    elif approver is not None:
        raise bad()
    if request['requested_by'] != request['requested_by'].strip().lower():
        raise bad()
    return request


def _strings(value):
    """Every string in a parsed JSON value, dict keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _real_day(value):
    """A DAY: the pattern, then a real calendar date (date.fromisoformat, never a FormatChecker)."""
    if not isinstance(value, str) or not DAY.match(value):
        raise ValueError('not a day')
    return date.fromisoformat(value)


def check_palette_request(request):
    """Step 4 of POST /v1/commands: the v2 rules of contract 14.3.1 and 14.3.2 (rules 2 to 5) that
    the JSON Schema cannot say. The shape of each argument (rule 1) is the subject's JSON Schema at
    step 7; a refusal there is also 422 REQUEST_INVALID. The bridge authorizes nothing."""
    def bad():
        return Refusal(422, 'REQUEST_INVALID')
    if set(request) != set(PALETTE_FIELDS):
        raise bad()
    version = request['schema_version']
    if isinstance(version, bool) or not isinstance(version, int) or version != 2:
        raise bad()
    # rule 5 first: no string anywhere in the request holds a lone surrogate.
    if any(LONE_SURROGATE.search(text) for text in _strings(request)):
        raise bad()
    subject = request['subject']
    if not isinstance(subject, str):
        raise bad()
    if subject not in ROUTES:
        raise Refusal(422, 'SUBJECT_UNKNOWN')
    text = ('command_id', 'client_request_id', 'command', 'client', 'mode', 'requested_by', 'session_id', 'issued_at')
    if not all(isinstance(request[f], str) for f in text):
        raise bad()
    command, client = request['command'], request['client']
    if command != ROUTES[subject]:
        raise bad()
    if not UUID.match(request['client_request_id']) or not 1 <= len(request['session_id']) <= 200:
        raise bad()
    if request['command_id'] != command_id_for(request['session_id'], request['client_request_id']):
        raise bad()
    if request['mode'] not in ('plan', 'apply') or not INSTANT.match(request['issued_at']):
        raise bad()
    if not SLUG.match(client) or len(client) > 64:
        raise bad()
    requested_by = request['requested_by']
    if requested_by != requested_by.strip().lower() or not 1 <= len(requested_by) <= PALETTE_MAX_EMAIL:
        raise bad()
    args = request['args']
    if not isinstance(args, dict):
        raise bad()
    # rule 4: no C0 control character or DEL in any string of args (values, list items, keys).
    if any(CONTROL.search(text) for text in _strings(args)):
        raise bad()
    # rule 3: where the entry takes `client`, it equals the request's; no other entry takes it.
    if command in CLIENT_COMMANDS:
        if args.get('client') != client:
            raise bad()
    elif 'client' in args:
        raise bad()
    # real calendar dates, and rule 2: the span.
    try:
        start = _real_day(args['date_from']) if 'date_from' in args else None
        end = _real_day(args['date_to']) if 'date_to' in args else None
    except ValueError:
        raise bad() from None
    if command in SPAN_COMMANDS:
        if 'period' in args:
            if start is not None or end is not None:
                raise bad()
        elif start is None or end is None or end < start:
            raise bad()
    elif start is not None or end is not None:
        raise bad()
    return request


def issued_at(request):
    try:
        return datetime.strptime(request['issued_at'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=UTC)
    except ValueError:  # 13th month, 25th hour: the pattern allows what the calendar does not
        raise Refusal(422, 'REQUEST_INVALID') from None


def check_fresh(request, now):
    """Step 5: signed within five minutes of this clock, either direction."""
    if abs(now - issued_at(request)) > WINDOW:
        raise Refusal(422, 'COMMAND_EXPIRED')


def envelope(request, key_id, signature, body_sha256, received_at):
    """The Create Invoice command envelope (contract section 5). The Worker never sees it."""
    return _envelope(TYPE, SUBJECT, request, key_id, signature, body_sha256, received_at)


def palette_envelope(request, key_id, signature, body_sha256, received_at):
    """The palette command envelope (contract 14.4.2): section 5's, typed by the signed subject."""
    subject = request['subject']
    return _envelope(subject.replace('.cmd.', '.', 1), subject, request, key_id, signature, body_sha256, received_at)


def _envelope(type_, subject, request, key_id, signature, body_sha256, received_at):
    command_id = request['command_id']
    action = type_.removeprefix('bloodbank.')  # billing.<entity>.<verb>
    return {
        'specversion': '1.0',
        'id': command_id,
        'source': 'urn:33god:service:farm-command-bridge',
        'type': type_,
        'subject': subject,
        'time': received_at,
        'datacontenttype': 'application/json',
        'dataschema': f'apicurio://holyfields/{type_}/versions/1',
        'schemaref': f'{type_}.v1',
        'kind': 'command',
        'domain': 'billing',
        'producer': 'farm-command-bridge',
        'service': 'farm-command-bridge',
        # The CLAIMED actor: the consumer trusts it only after it re-verifies the signature.
        'actor': {'type': 'operator', 'agent_id': 'farm:' + request['requested_by']},
        'correlationid': command_id,
        'causationid': None,
        'command_id': command_id,
        'idempotency_key': f"{action}:client:{request['client']}:command:{command_id}",
        'delivery': 'single_consumer',
        'data': {
            'schema_version': 1,
            'request': request,
            'signature': {'alg': 'hmac-sha256', 'key_id': key_id, 'value': signature},
            'body_sha256': body_sha256,
            'received_at': received_at,
        },
    }


# ---------------------------------------------------------------------------------------------
# Mercury webhooks (Milkshake Story 9.6, docs/farm-commands.md section 16).

class EventUnreadable(Exception):
    """An authentic Mercury body that fails a required rule (16.3.2 step 3): archived, never 4xx."""

    def __init__(self, event_id=None):
        super().__init__('EVENT_UNREADABLE')
        self.event_id = event_id


def mercury_secrets_from_env(environ):
    """MERCURY_WEBHOOK_ENDPOINT_<n> with MERCURY_WEBHOOK_SECRET_<n>, n in 1 and 2: the endpoints whose
    signing secrets verify a delivery, in the order they are tried. None configured means the route
    answers 503 HOOK_UNCONFIGURED; half a pair is a startup error (contract 16.3.1)."""
    pairs = []
    for slot in MERCURY_ENDPOINT_SLOTS:
        endpoint_id = (environ.get(f'MERCURY_WEBHOOK_ENDPOINT_{slot}') or '').strip().lower()
        secret = environ.get(f'MERCURY_WEBHOOK_SECRET_{slot}') or ''
        if not endpoint_id and not secret:
            continue
        if not endpoint_id or not secret:
            raise ValueError(f'MERCURY_WEBHOOK_ENDPOINT_{slot} and MERCURY_WEBHOOK_SECRET_{slot} are set together or not at all')
        if not UUID.match(endpoint_id):
            raise ValueError(f'MERCURY_WEBHOOK_ENDPOINT_{slot} is not a uuid')
        if secret.startswith('op://'):
            raise ValueError(f'MERCURY_WEBHOOK_SECRET_{slot} is an unresolved op:// reference')
        pairs.append((endpoint_id, secret))
    if len({endpoint_id for endpoint_id, _ in pairs}) != len(pairs):
        raise ValueError('MERCURY_WEBHOOK_ENDPOINT_1 and _2 name the same endpoint')
    return tuple(pairs)


def hook_key_id(keys):
    """The key the Mercury route signs with: the greatest key id of the mercury-hook- purpose."""
    ids = sorted(key_id for key_id in keys if key_id.startswith(MERCURY_KEY_PREFIX))
    return ids[-1] if ids else None


def parse_mercury_signature(header):
    """Mercury-Signature: t=<unix s>,v1=<hex>[,v1=<hex>...] -> (t, [v1, ...]) or None.

    Split on ',', strip ASCII whitespace around each part, split each part at its first '='.
    Exactly one t of 1 to 12 digits, one or more v1 of 64 hex digits (lowercased); any other key
    is ignored (M8, M10)."""
    if not isinstance(header, str):
        return None
    stamps, signatures = [], []
    for part in header.split(','):
        name, sep, value = part.strip(' \t\r\n\x0b\x0c').partition('=')
        if not sep:
            continue
        if name == 't':
            stamps.append(value)
        elif name == 'v1':
            signatures.append(value.lower())
    if len(stamps) != 1 or not MERCURY_T.match(stamps[0]):
        return None
    if not signatures or not all(MERCURY_V1.match(value) for value in signatures):
        return None
    return stamps[0], signatures


def verify_mercury(raw, header, secrets, now):
    """Step 2: (verdict, endpoint_id, matched_endpoint_id), before anything is parsed.

    verdict is ok, SIGNATURE_MISSING, SIGNATURE_INVALID or SIGNATURE_EXPIRED; endpoint_id is set
    only for ok (the golden signature_cases pin both); matched_endpoint_id names the endpoint whose
    secret verified, for ok and SIGNATURE_EXPIRED. HMAC-SHA256 with the secret's UTF-8 bytes over
    "<t>.<raw body>", compared in constant time; secrets in order _1, _2, each v1 in turn."""
    parsed = parse_mercury_signature(header)
    if parsed is None:
        return 'SIGNATURE_MISSING', None, None
    stamp_text, signatures = parsed
    signed = stamp_text.encode('ascii') + b'.' + raw
    matched = None
    for endpoint_id, secret in secrets:
        expected = hmac.new(secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, value) for value in signatures):
            matched = endpoint_id
            break
    if matched is None:
        return 'SIGNATURE_INVALID', None, None
    if abs(now - int(stamp_text)) > MERCURY_WINDOW_S:
        return 'SIGNATURE_EXPIRED', None, matched
    return 'ok', matched, matched


def _mercury_uuid(value):
    if not isinstance(value, str):
        return None
    value = value.lower()
    return value if UUID.match(value) else None


def _mercury_instant(value):
    """An ISO 8601 instant with an offset, as the six-digit UTC spelling; None otherwise."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return None
        text = stamp(moment)
    except (ValueError, OverflowError):
        return None
    return text if INSTANT.match(text) else None


def _mercury_version(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _mercury_paths(value):
    """changedPaths: field names only, unique and sorted; anything else makes the whole value None."""
    if not isinstance(value, list) or len(value) > MERCURY_MAX_PATHS:
        return None
    if not all(isinstance(item, str) and MERCURY_PATH_NAME.match(item) for item in value):
        return None
    return sorted(set(value))


def read_mercury_event(raw):
    """Step 3: the four required fields, and the optional ones as None when absent or unreadable.

    Required: id and resourceId (lowercased uuids), resourceType (1 to 64 characters),
    operationType (create, update or delete). Optional: occurredAt, resourceVersion, changedPaths,
    mergePatch.createdAt and mergePatch.postedAt (M6: whether a create carries changedPaths is
    silent, so none of them refuses). Raises EventUnreadable carrying the id when it alone read."""
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise EventUnreadable() from None
    if not isinstance(value, dict):
        raise EventUnreadable()
    event_id = _mercury_uuid(value.get('id'))
    resource_id = _mercury_uuid(value.get('resourceId'))
    resource_type, operation = value.get('resourceType'), value.get('operationType')
    if (event_id is None or resource_id is None or not isinstance(resource_type, str)
            or not 1 <= len(resource_type) <= 64 or not isinstance(operation, str)
            or operation not in MERCURY_OPERATIONS):
        raise EventUnreadable(event_id)
    patch = value.get('mergePatch')
    patch = patch if isinstance(patch, dict) else {}
    return {
        'event_id': event_id,
        'resource_type': resource_type,
        'resource_id': resource_id,
        'operation_type': operation,
        'resource_version': _mercury_version(value.get('resourceVersion')),
        'occurred_at': _mercury_instant(value.get('occurredAt')),
        'created_at': _mercury_instant(patch.get('createdAt')),
        'posted_at': _mercury_instant(patch.get('postedAt')),
        'changed_paths': _mercury_paths(value.get('changedPaths')),
    }


def hook_command_id(event_id):
    return str(uuid.uuid5(NS_MERCURY_HOOK, 'event:' + event_id))


def hook_request(event, endpoint_id, mercury_t, mercury_body_sha256, received_at):
    """The minimal request the bridge signs (contract 16.4): every key always present, ids, types
    and instants only. Never an amount, a description, a counterparty, a note or the patch."""
    return {
        'schema_version': 1,
        'command_id': hook_command_id(event['event_id']),
        'endpoint_id': endpoint_id,
        'event_id': event['event_id'],
        'resource_type': event['resource_type'],
        'resource_id': event['resource_id'],
        'operation_type': event['operation_type'],
        'resource_version': event['resource_version'],
        'occurred_at': event['occurred_at'],
        'created_at': event['created_at'],
        'posted_at': event['posted_at'],
        'changed_paths': event['changed_paths'],
        'mercury_t': mercury_t,
        'mercury_body_sha256': mercury_body_sha256,
        'received_at': received_at,
    }


def hook_envelope(request, key_id, signature, body_sha256):
    """The bloodbank.billing.transaction.receive envelope (contract 16.4): Mercury, as a service,
    is the actor; the bridge's mercury-hook- key signs canonical(request)."""
    command_id, event_id = request['command_id'], request['event_id']
    return {
        'specversion': '1.0',
        'id': command_id,
        'source': 'urn:33god:service:farm-command-bridge',
        'type': MERCURY_TYPE,
        'subject': MERCURY_SUBJECT,
        'time': request['received_at'],
        'datacontenttype': 'application/json',
        'dataschema': f'apicurio://holyfields/{MERCURY_TYPE}/versions/1',
        'schemaref': f'{MERCURY_TYPE}.v1',
        'kind': 'command',
        'domain': 'billing',
        'producer': 'farm-command-bridge',
        'service': 'farm-command-bridge',
        'actor': {'type': 'service', 'agent_id': 'mercury-webhook'},
        'correlationid': command_id,
        'causationid': None,
        'command_id': command_id,
        'idempotency_key': f'billing.transaction.receive:mercury_event:{event_id}:command:{command_id}',
        'delivery': 'single_consumer',
        'data': {
            'schema_version': 1,
            'request': request,
            'signature': {'alg': 'hmac-sha256', 'key_id': key_id, 'value': signature},
            'body_sha256': body_sha256,
        },
    }


def build_hook(event, endpoint_id, mercury_t, mercury_body_sha256, received_at, key_id, key):
    """Step 7, in memory and before any write: request, signature, envelope, schema. Raises on a
    validation failure (a bridge bug: archived ignored ENVELOPE_INVALID, never a 4xx)."""
    request = hook_request(event, endpoint_id, mercury_t, mercury_body_sha256, received_at)
    text = canonical(request).encode('utf-8')
    event = hook_envelope(request, key_id, sign(key, text), sha256_hex(text))
    validate_envelope(event)
    return event


def read_chunked(stream, cap):
    """Decode a Transfer-Encoding: chunked body (BaseHTTPRequestHandler does not). More than cap
    bytes of body is 413 BODY_TOO_LARGE, decided before the chunk that would pass it is read;
    broken framing is ValueError (503 BODY_UNREADABLE)."""
    body = bytearray()
    while True:
        line = stream.readline(MAX_CHUNK_LINE + 1)
        if not line.endswith(b'\n'):
            raise ValueError('chunk size line')
        size_text = line.rstrip(b'\r\n').split(b';', 1)[0].strip(b' \t')
        if not CHUNK_SIZE.match(size_text):
            raise ValueError('chunk size')
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > cap:
            raise Refusal(413, 'BODY_TOO_LARGE')
        chunk = stream.read(size)
        if len(chunk) != size or stream.read(2) != b'\r\n':
            raise ValueError('chunk data')
        body += chunk
    for _ in range(MAX_TRAILERS):
        line = stream.readline(MAX_CHUNK_LINE + 1)
        if not line.endswith(b'\n'):
            raise ValueError('trailer')
        if line in (b'\r\n', b'\n'):
            return bytes(body)
    raise ValueError('trailers')


class Archive:
    """command_id -> the signed request and its envelope, immutable; delivery state beside it."""

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS commands (
                  command_id TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL, body BLOB NOT NULL,
                  envelope TEXT NOT NULL, received_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery (
                  command_id TEXT PRIMARY KEY REFERENCES commands(command_id),
                  published INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                CREATE TRIGGER IF NOT EXISTS immutable_command_update BEFORE UPDATE ON commands
                  BEGIN SELECT RAISE(ABORT,'immutable farm command'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_command_delete BEFORE DELETE ON commands
                  BEGIN SELECT RAISE(ABORT,'immutable farm command'); END;''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, command_id, raw, event):
        """Insert, or report the prior row: ('new'|'duplicate', envelope, published)."""
        digest = sha256_hex(raw)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT c.body_sha256, c.envelope, d.published FROM commands c JOIN delivery d USING(command_id) WHERE command_id=?', (command_id,)).fetchone()
            if prior:
                if not hmac.compare_digest(prior[0], digest):
                    raise Refusal(409, 'COMMAND_ID_CONFLICT')
                return 'duplicate', json.loads(prior[1]), bool(prior[2])
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?)', (command_id, digest, raw, canonical(event), event['time']))
            db.execute('INSERT INTO delivery(command_id) VALUES(?)', (command_id,))
            return 'new', event, False

    def mark(self, command_id, *, published=None, error=None):
        values = {k: v for k, v in (('published', published), ('error', error)) if v is not None}
        with self.connect() as db:
            db.execute('UPDATE delivery SET ' + ','.join(k + '=?' for k in values) + ' WHERE command_id=?', (*values.values(), command_id))

    def pending(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT c.envelope FROM commands c JOIN delivery d USING(command_id) WHERE d.published=0 ORDER BY c.rowid')]

    def get(self, command_id):
        with self.connect() as db:
            row = db.execute('SELECT envelope FROM commands WHERE command_id=?', (command_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def health(self):
        with self.connect() as db:
            archived, unpublished, errors = db.execute("SELECT count(*), coalesce(sum(published=0),0), coalesce(sum(error!=''),0) FROM delivery").fetchone()
            last = db.execute("SELECT error FROM delivery WHERE error!='' ORDER BY rowid DESC LIMIT 1").fetchone()
        return {'archived': archived, 'unpublished': unpublished, 'errors': errors, 'last_error': last[0] if last else None}


class MercuryArchive:
    """Mercury deliveries in the same SQLite file as the Farm commands (contract 16.3.3).

    mercury_events holds only built events (every row has its envelope), mercury_ignored and
    mercury_conflicts are append-only evidence; only mercury_delivery.state and .error change
    (pending to published). The request path's connections wait at most 1.5 s for a lock, so a
    locked archive answers 503 ARCHIVE_UNAVAILABLE inside Mercury's 5 s."""

    def __init__(self, path, busy_timeout=MERCURY_BUSY_TIMEOUT_S):
        self.path, self.busy_timeout = str(path), busy_timeout
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        codes = ','.join(f"'{code}'" for code in MERCURY_IGNORED_CODES)
        with self.connect(timeout=15) as db:
            db.executescript(f'''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS mercury_events (
                  event_id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL, body_sha256 TEXT NOT NULL,
                  body BLOB NOT NULL, mercury_t INTEGER NOT NULL, received_at TEXT NOT NULL,
                  command_id TEXT NOT NULL UNIQUE, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mercury_delivery (
                  event_id TEXT PRIMARY KEY REFERENCES mercury_events(event_id),
                  state TEXT NOT NULL CHECK (state IN ('pending','published')),
                  error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS mercury_ignored (
                  rowid INTEGER PRIMARY KEY, event_id TEXT, endpoint_id TEXT NOT NULL,
                  body_sha256 TEXT NOT NULL, body BLOB NOT NULL, mercury_t INTEGER NOT NULL,
                  received_at TEXT NOT NULL, code TEXT NOT NULL CHECK (code IN ({codes})));
                CREATE TABLE IF NOT EXISTS mercury_conflicts (
                  rowid INTEGER PRIMARY KEY, event_id TEXT NOT NULL, body_sha256 TEXT NOT NULL,
                  body BLOB NOT NULL, received_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS mercury_ignored_received ON mercury_ignored(received_at);
                CREATE TRIGGER IF NOT EXISTS immutable_mercury_event_update BEFORE UPDATE ON mercury_events
                  BEGIN SELECT RAISE(ABORT,'immutable mercury event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_mercury_event_delete BEFORE DELETE ON mercury_events
                  BEGIN SELECT RAISE(ABORT,'immutable mercury event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_mercury_ignored_update BEFORE UPDATE ON mercury_ignored
                  BEGIN SELECT RAISE(ABORT,'immutable mercury evidence'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_mercury_ignored_delete BEFORE DELETE ON mercury_ignored
                  BEGIN SELECT RAISE(ABORT,'immutable mercury evidence'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_mercury_conflict_update BEFORE UPDATE ON mercury_conflicts
                  BEGIN SELECT RAISE(ABORT,'immutable mercury evidence'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_mercury_conflict_delete BEFORE DELETE ON mercury_conflicts
                  BEGIN SELECT RAISE(ABORT,'immutable mercury evidence'); END;
                CREATE TRIGGER IF NOT EXISTS mercury_delivery_forward BEFORE UPDATE ON mercury_delivery
                  WHEN NEW.event_id != OLD.event_id OR (OLD.state = 'published' AND NEW.state != 'published')
                  BEGIN SELECT RAISE(ABORT,'mercury delivery moves pending to published only'); END;
                CREATE TRIGGER IF NOT EXISTS mercury_delivery_kept BEFORE DELETE ON mercury_delivery
                  BEGIN SELECT RAISE(ABORT,'mercury delivery is kept'); END;''')

    @contextmanager
    def connect(self, timeout=None):
        db = sqlite3.connect(self.path, timeout=self.busy_timeout if timeout is None else timeout)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _ignore(db, event_id, endpoint_id, raw, mercury_t, received_at, code):
        db.execute('INSERT INTO mercury_ignored(event_id, endpoint_id, body_sha256, body, mercury_t, received_at, code) '
                   'VALUES(?,?,?,?,?,?,?)', (event_id, endpoint_id, sha256_hex(raw), raw, mercury_t, received_at, code))

    def ignore(self, event_id, endpoint_id, raw, mercury_t, received_at, code):
        """An authentic body that is not acted on (step 3): evidence, never a dedupe key."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._ignore(db, event_id, endpoint_id, raw, mercury_t, received_at, code)

    def receive(self, *, event_id, endpoint_id, raw, mercury_t, received_at, expired, resource_ok, envelope):
        """Steps 4 to 7 in one transaction: dedupe by event id first, whatever t says; then the
        window, the resource type and the build, each archived ignored with its code; else one
        insert of the built event and its pending delivery.

        Returns ('duplicate', envelope, published) | ('conflict', None, None) |
        ('ignored', code, None) | ('new', envelope, False)."""
        digest = sha256_hex(raw)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT e.body_sha256, e.envelope, d.state FROM mercury_events e '
                               'JOIN mercury_delivery d USING(event_id) WHERE event_id=?', (event_id,)).fetchone()
            if prior:
                if hmac.compare_digest(prior[0], digest):
                    return 'duplicate', json.loads(prior[1]), prior[2] == 'published'
                db.execute('INSERT INTO mercury_conflicts(event_id, body_sha256, body, received_at) VALUES(?,?,?,?)',
                           (event_id, digest, raw, received_at))
                return 'conflict', None, None
            code = ('SIGNATURE_EXPIRED' if expired else 'RESOURCE_IGNORED' if not resource_ok
                    else 'ENVELOPE_INVALID' if envelope is None else None)
            if code:
                self._ignore(db, event_id, endpoint_id, raw, mercury_t, received_at, code)
                return 'ignored', code, None
            db.execute('INSERT INTO mercury_events VALUES(?,?,?,?,?,?,?,?)',
                       (event_id, endpoint_id, digest, raw, mercury_t, received_at, envelope['id'], canonical(envelope)))
            db.execute("INSERT INTO mercury_delivery(event_id, state) VALUES(?, 'pending')", (event_id,))
            return 'new', envelope, False

    def mark(self, event_id, *, published=False, error=None):
        sets, values = [], []
        if published:
            sets.append("state='published'")
        if error is not None:
            sets.append('error=?')
            values.append(error)
        with self.connect() as db:
            db.execute('UPDATE mercury_delivery SET ' + ','.join(sets) + ' WHERE event_id=?', (*values, event_id))

    def pending(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT e.envelope FROM mercury_events e JOIN mercury_delivery d USING(event_id) "
                "WHERE d.state='pending' ORDER BY e.rowid")]

    def get(self, event_id):
        with self.connect() as db:
            row = db.execute('SELECT envelope FROM mercury_events WHERE event_id=?', (event_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def health(self, now):
        """The mercury_* readiness fields. An alarm is an authentic event dropped until the
        backstop (EVENT_UNREADABLE, ENVELOPE_INVALID or SIGNATURE_EXPIRED) in the last 24 hours;
        RESOURCE_IGNORED never alarms."""
        cutoff = stamp(now - MERCURY_ALARM_WINDOW)
        marks = ','.join('?' for _ in MERCURY_ALARM_CODES)
        with self.connect() as db:
            archived, unpublished = db.execute(
                "SELECT count(*), coalesce(sum(state='pending'),0) FROM mercury_delivery").fetchone()
            ignored = dict(db.execute('SELECT code, count(*) FROM mercury_ignored GROUP BY code').fetchall())
            last = db.execute('SELECT max(received_at) FROM (SELECT max(received_at) AS received_at FROM mercury_events '
                              'UNION ALL SELECT max(received_at) FROM mercury_ignored '
                              'UNION ALL SELECT max(received_at) FROM mercury_conflicts)').fetchone()[0]
            alarm = db.execute(f'SELECT code, received_at, event_id FROM mercury_ignored WHERE code IN ({marks}) '
                               'AND received_at >= ? ORDER BY received_at DESC, rowid DESC LIMIT 1',
                               (*MERCURY_ALARM_CODES, cutoff)).fetchone()
        return {
            'mercury_archived': archived,
            'mercury_unpublished': unpublished,
            'mercury_ignored': {code: ignored.get(code, 0) for code in MERCURY_IGNORED_CODES},
            'mercury_last_received_at': last,
            'mercury_alarm': {'code': alarm[0], 'received_at': alarm[1], 'event_id': alarm[2]} if alarm else None,
        }


def log(**fields):
    print(canonical(fields), flush=True)


class Bridge:
    def __init__(self, archive, keys, js, broker_connected=lambda: True, clock=lambda: datetime.now(UTC),
                 mercury_archive=None, mercury_secrets=()):
        self.archive, self.keys, self.js = archive, keys, js
        self.broker_connected, self.clock = broker_connected, clock
        self.mercury_archive, self.mercury_secrets = mercury_archive, tuple(mercury_secrets)
        self.mercury_key_id = hook_key_id(keys)

    @property
    def mercury_configured(self):
        """The Mercury route needs an archive, at least one endpoint secret and a mercury-hook- key."""
        return bool(self.mercury_archive is not None and self.mercury_secrets and self.mercury_key_id)

    def health(self):
        """Liveness is answering at all; readiness (broker up, archive drained) is in the body.

        The container healthcheck reads only liveness, so Traefik keeps routing through a NATS
        blip: a POST then answers 503 BUS_UNAVAILABLE with the command archived, and the sweep
        publishes it once the broker is back. With the Mercury archive, readiness also needs every
        hook delivery published and no alarm (an authentic Mercury event dropped in the last 24 h).
        """
        try:
            state = self.archive.health()
        except Exception as exc:
            state = {'archived': None, 'unpublished': None, 'errors': None,
                     'last_error': 'archive:' + type(exc).__name__}
        state['live'] = True
        state['broker_connected'] = bool(self.broker_connected())
        ready = state['broker_connected'] and state['unpublished'] == 0
        if self.mercury_archive is not None:
            state['mercury_configured'] = self.mercury_configured
            try:
                state.update(self.mercury_archive.health(self.clock()))
            except Exception as exc:
                state.update({'mercury_archived': None, 'mercury_unpublished': None, 'mercury_ignored': None,
                              'mercury_last_received_at': None, 'mercury_alarm': None,
                              'last_error': state['last_error'] or 'mercury_archive:' + type(exc).__name__})
            ready = ready and state['mercury_unpublished'] == 0 and state['mercury_alarm'] is None
        state['ready'] = ready
        return state

    async def publish(self, event):
        command_id = event['id']
        try:
            receipt = await self.js.publish(event['subject'], canonical(event).encode('utf-8'), headers={'Nats-Msg-Id': command_id}, timeout=10)
            if receipt.stream != STREAM:
                raise ValueError('Wrong durable stream')
        except Exception as exc:
            self.archive.mark(command_id, error='publish:' + type(exc).__name__)
            raise
        self.archive.mark(command_id, published=1, error='')
        return receipt

    async def publish_mercury(self, event, timeout=10):
        """Publish one archived hook envelope; mark its delivery published, or keep it pending
        with its error for the sweep."""
        event_id = event['data']['request']['event_id']
        try:
            receipt = await self.js.publish(event['subject'], canonical(event).encode('utf-8'),
                                            headers={'Nats-Msg-Id': event['id']}, timeout=timeout)
            if receipt.stream != STREAM:
                raise ValueError('Wrong durable stream')
        except Exception as exc:
            try:
                self.mercury_archive.mark(event_id, error='publish:' + type(exc).__name__)
            except Exception:
                pass
            raise
        try:
            self.mercury_archive.mark(event_id, published=True, error='')
        except Exception as exc:  # published; the sweep re-sends it and JetStream dedupes on the id
            log(event_id=event_id, command_id=event['id'], status='mark_failed', code=type(exc).__name__)
        return receipt

    def accept(self, raw, key_id, signature_header):
        """Create Invoice, steps 1 to 6, synchronous: (status, event, published). Raises Refusal."""
        return self._accept(raw, key_id, signature_header, INVOICE_KEY_PREFIX, check_request, envelope)

    def accept_palette(self, raw, key_id, signature_header):
        """POST /v1/commands, steps 1 to 6 (contract 14.4.1): (status, event, published)."""
        return self._accept(raw, key_id, signature_header, PALETTE_KEY_PREFIX, check_palette_request, palette_envelope)

    def _accept(self, raw, key_id, signature_header, prefix, check, build):
        if len(raw) > MAX_BODY:
            raise Refusal(413, 'BODY_TOO_LARGE')
        signature = verify(raw, key_id, signature_header, self.keys, prefix)
        request = check(parse(raw))
        now = self.clock()
        check_fresh(request, now)
        event = build(request, key_id, signature, sha256_hex(raw), stamp(now))
        try:
            validate_envelope(event)
        except Exception:
            raise Refusal(422, 'REQUEST_INVALID') from None
        return self.archive.save(request['command_id'], raw, event)

    def accept_mercury(self, raw, signature_header):
        """POST /v1/mercury-webhooks, steps 2 to 7 (contract 16.3.2), synchronous.

        Returns (http_status, response_body, envelope_to_publish_or_None, log_fields). Raises
        Refusal only for 401 (a body that is not authentic) and 503 ARCHIVE_UNAVAILABLE: once
        the HMAC matches, every other outcome is a 2xx."""
        if len(raw) > MERCURY_MAX_BODY:
            raise Refusal(413, 'BODY_TOO_LARGE')
        moment = self.clock()
        received_at = stamp(moment)
        verdict, _endpoint_id, matched = verify_mercury(raw, signature_header, self.mercury_secrets, moment.timestamp())
        if verdict in ('SIGNATURE_MISSING', 'SIGNATURE_INVALID'):
            raise Refusal(401, verdict)
        mercury_t = int(parse_mercury_signature(signature_header)[0])
        fields = {'endpoint_id': matched}
        try:
            event = read_mercury_event(raw)
        except EventUnreadable as unreadable:
            event_id = fields['event_id'] = unreadable.event_id
            self._mercury_write(lambda: self.mercury_archive.ignore(
                event_id, matched, raw, mercury_t, received_at, 'EVENT_UNREADABLE'), fields)
            return 200, {'success': True, 'ignored': True, 'code': 'EVENT_UNREADABLE'}, None, {**fields, 'code': 'EVENT_UNREADABLE'}
        fields['event_id'] = event['event_id']
        resource_ok = event['resource_type'] == 'transaction'
        built = None
        if verdict == 'ok' and resource_ok:
            try:
                built = build_hook(event, matched, mercury_t, sha256_hex(raw), received_at,
                                   self.mercury_key_id, self.keys[self.mercury_key_id])
            except Exception as exc:  # a bridge bug: archived ignored, logged, never a 4xx
                log(**fields, status='envelope_invalid', code=type(exc).__name__)
        outcome, detail, published = self._mercury_write(lambda: self.mercury_archive.receive(
            event_id=event['event_id'], endpoint_id=matched, raw=raw, mercury_t=mercury_t,
            received_at=received_at, expired=verdict == 'SIGNATURE_EXPIRED', resource_ok=resource_ok,
            envelope=built), fields)
        if outcome == 'duplicate':
            fields['command_id'] = detail['id']
            body = {'success': True, 'duplicate': True, 'command_id': detail['id']}
            return 200, body, None if published else detail, {**fields, 'code': 'DUPLICATE'}
        if outcome == 'conflict':
            return 200, {'success': True, 'duplicate': True, 'conflict': True}, None, {**fields, 'code': 'EVENT_ID_CONFLICT'}
        if outcome == 'ignored':
            return 200, {'success': True, 'ignored': True, 'code': detail}, None, {**fields, 'code': detail}
        fields['command_id'] = detail['id']
        body = {'success': True, 'duplicate': False, 'command_id': detail['id'], 'published': False}
        return 202, body, detail, {**fields, 'code': 'ACCEPTED'}

    @staticmethod
    def _mercury_write(write, fields):
        try:
            return write()
        except (sqlite3.Error, OSError) as exc:  # a lock past the 1.5 s busy timeout, a disk error
            log(**fields, status='archive_failed', code=type(exc).__name__)
            raise Refusal(503, 'ARCHIVE_UNAVAILABLE') from None

    async def sweep(self):
        """Publish every archived, unpublished command and every pending hook delivery; one that
        fails never holds up the rest.

        Skipped while the broker is down (every publish would wait out its timeout). A row that
        cannot publish is logged and marked with its error, and the sweep moves on to the next.
        """
        if not self.broker_connected():
            return
        failed = False
        try:
            pending = list(self.archive.pending())
        except Exception as exc:
            log(status='archive_pending_failed', code=type(exc).__name__)
            pending, failed = [], True
        for event in pending:
            try:
                await self.publish(event)
                log(command_id=event['id'], status='republished')
            except Exception as exc:
                log(command_id=event['id'], status='republish_failed', code=type(exc).__name__)
        if self.mercury_archive is not None:
            try:
                hooks = list(self.mercury_archive.pending())
            except Exception as exc:
                log(status='mercury_pending_failed', code=type(exc).__name__)
                hooks, failed = [], True
            for event in hooks:
                fields = {'event_id': event['data']['request']['event_id'], 'command_id': event['id']}
                try:
                    await self.publish_mercury(event)
                    log(**fields, status='republished')
                except Exception as exc:
                    log(**fields, status='republish_failed', code=type(exc).__name__)
        if failed:
            await asyncio.sleep(15)


def request_host(headers):
    """The Host header, lowercased, without its port."""
    return re.sub(r':[0-9]*\Z', '', (headers.get('Host') or '').strip().lower())


def handler(bridge, loop):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, value):
            body = canonical(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def refuse(self, refusal, command_id=None, **fields):
            log(command_id=command_id, **fields, status=refusal.status, code=refusal.code)
            return self.respond(refusal.status, {'success': False, 'error': refusal.code, 'code': refusal.code})

        def do_GET(self):
            # /healthz is liveness (always 200 while the process serves; readiness in the body),
            # which is what the container healthcheck and so Traefik's routing read. /readyz is
            # the same body with 503 while not ready, for monitors that alert on a status code.
            if self.path not in ('/healthz', '/readyz'):
                return self.respond(404, {'error': 'Not found'})
            state = bridge.health()
            ready_code = 200 if state['ready'] else 503
            return self.respond(200 if self.path == '/healthz' else ready_code, state)

        def do_POST(self):
            # Two trust sources, two hosts: the Mercury path answers only on its own host, and the
            # Farm paths never answer there (contract 16.3.2 step 0).
            host = request_host(self.headers)
            if self.path == MERCURY_PATH:
                if host != MERCURY_HOST:
                    return self.respond(404, {'error': 'Not found'})
                return self.mercury()
            accept = {PATH: bridge.accept, PALETTE_PATH: bridge.accept_palette}.get(self.path)
            if accept is None or host == MERCURY_HOST:
                return self.respond(404, {'error': 'Not found'})
            try:
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise Refusal(400, 'BODY_INVALID') from None
                if size > MAX_BODY:
                    raise Refusal(413, 'BODY_TOO_LARGE')
                if size < 1 or self.headers.get_content_type() != 'application/json':
                    raise Refusal(400, 'BODY_INVALID')
                self.connection.settimeout(15)
                raw = self.rfile.read(size)
                status, event, published = accept(raw, self.headers.get('X-Milkshake-Key-Id'), self.headers.get('X-Milkshake-Signature'))
            except Refusal as refusal:
                return self.refuse(refusal)
            command_id = event['id']
            if not published:
                try:
                    asyncio.run_coroutine_threadsafe(bridge.publish(event), loop).result(timeout=20)
                except Exception:
                    return self.refuse(Refusal(503, 'BUS_UNAVAILABLE'), command_id)
            duplicate = status == 'duplicate'
            log(command_id=command_id, subject=event['subject'], status=200 if duplicate else 202, code='DUPLICATE' if duplicate else 'ACCEPTED')
            return self.respond(200 if duplicate else 202, {
                'success': True, 'duplicate': duplicate, 'command_id': command_id, 'received_at': event['time'],
            })

        def read_mercury_body(self):
            """Step 1: Content-Length 1 to 65536, or chunked (cloudflared may forward that way);
            more is 413; anything unreadable, including a 3 s stall, is 503 BODY_UNREADABLE so
            that Mercury retries. Content-Type is not checked (Mercury does not document it)."""
            try:
                self.connection.settimeout(MERCURY_READ_TIMEOUT_S)
                encoding = self.headers.get('Transfer-Encoding')
                if encoding is not None:
                    if encoding.strip().lower() != 'chunked':
                        raise ValueError('transfer encoding')
                    raw = read_chunked(self.rfile, MERCURY_MAX_BODY)
                else:
                    length = (self.headers.get('Content-Length') or '').strip()
                    if not re.fullmatch(r'[0-9]+', length):
                        raise ValueError('content length')
                    size = int(length)
                    if size > MERCURY_MAX_BODY:
                        raise Refusal(413, 'BODY_TOO_LARGE')
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError('short body')
            except (OSError, ValueError):
                raise Refusal(503, 'BODY_UNREADABLE') from None
            if not raw:
                raise Refusal(503, 'BODY_UNREADABLE')
            return raw

        def mercury(self):
            if not bridge.mercury_configured:
                return self.refuse(Refusal(503, 'HOOK_UNCONFIGURED'))
            try:
                raw = self.read_mercury_body()
                status, body, event, fields = bridge.accept_mercury(raw, self.headers.get('Mercury-Signature'))
            except Refusal as refusal:
                return self.refuse(refusal)
            if event is not None:  # new, or a duplicate whose delivery is still pending
                published = self.publish_mercury(event)
                if 'published' in body:
                    body['published'] = published
            log(**fields, status=status)
            return self.respond(status, body)

        def publish_mercury(self, event):
            """Step 8: at most 2 s for the PubAck. A miss leaves the delivery pending for the
            sweep; the answer is still 2xx, because the delivery is durably archived."""
            future = asyncio.run_coroutine_threadsafe(bridge.publish_mercury(event, timeout=MERCURY_PUBACK_TIMEOUT_S), loop)
            try:
                future.result(timeout=MERCURY_PUBACK_TIMEOUT_S + 0.25)
                return True
            except Exception as exc:
                future.cancel()
                event_id = event['data']['request']['event_id']
                try:
                    bridge.mercury_archive.mark(event_id, error='publish:' + type(exc).__name__)
                except Exception:
                    pass
                log(event_id=event_id, command_id=event['id'], status='publish_deferred', code=type(exc).__name__)
                return False
    return Handler


async def run(args):
    import nats
    keys = keys_from_env(os.environ['FARM_COMMAND_KEYS'])
    mercury_secrets = mercury_secrets_from_env(os.environ)  # a half pair stops the start here
    archive_path = os.environ.get('FARM_COMMAND_ARCHIVE_PATH', '/data/farm-command-archive.sqlite')
    archive = Archive(archive_path)
    mercury_archive = MercuryArchive(archive_path)
    nc = await nats.connect(os.environ.get('NATS_URL', 'nats://bloodbank-nats:4222'), name='farm-command-bridge', max_reconnect_attempts=-1)
    bridge = Bridge(archive, keys, nc.jetstream(), broker_connected=lambda: nc.is_connected,
                    mercury_archive=mercury_archive, mercury_secrets=mercury_secrets)
    if args.command == 'republish':
        if args.event_id:
            event = mercury_archive.get(args.event_id.strip().lower())
            if event is None:
                raise SystemExit('unknown event_id')
            receipt = await bridge.publish_mercury(event)
            print(canonical({'event_id': event['data']['request']['event_id'], 'command_id': event['id'], 'stream': receipt.stream, 'sequence': receipt.seq, 'duplicate': bool(getattr(receipt, 'duplicate', False))}))
        else:
            event = archive.get(args.command_id)
            if event is None:
                raise SystemExit('unknown command_id')
            receipt = await bridge.publish(event)
            print(canonical({'command_id': event['id'], 'stream': receipt.stream, 'sequence': receipt.seq, 'duplicate': bool(getattr(receipt, 'duplicate', False))}))
        await nc.drain()
        return
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), handler(bridge, asyncio.get_running_loop()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    unusable = sorted(k for k in keys if not k.startswith((INVOICE_KEY_PREFIX, PALETTE_KEY_PREFIX, MERCURY_KEY_PREFIX)))
    if unusable:
        log(status='key_purpose_unknown', key_ids=unusable)  # signs nothing: no route admits it
    if mercury_secrets and not bridge.mercury_key_id:
        log(status='mercury_key_missing', code='HOOK_UNCONFIGURED')  # no mercury-hook- key to sign with
    log(status='listening', key_ids=sorted(keys), mercury_endpoints=[e for e, _ in mercury_secrets],
        mercury_key_id=bridge.mercury_key_id)
    while True:
        await bridge.sweep()
        await asyncio.sleep(15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['serve', 'republish'], nargs='?', default='serve')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--command-id', help='republish one archived Farm command')
    target.add_argument('--event-id', help='republish one archived Mercury hook envelope (by Mercury event id)')
    parsed = parser.parse_args()
    if parsed.command == 'republish' and not (parsed.command_id or parsed.event_id):
        parser.error('republish needs --command-id or --event-id')
    asyncio.run(run(parsed))
