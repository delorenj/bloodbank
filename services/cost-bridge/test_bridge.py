import asyncio
import copy
from datetime import UTC, datetime
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import sqlite3
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

spec = importlib.util.spec_from_file_location('cost_bridge', Path(__file__).with_name('main.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def event(project='one', revision=1):
    data = dict(schema_version=1,client_id=project,project_id=project,provider='vendor',account_id='account-'+project,resource_id='phone',period_start='2026-09-01',period_end='2026-10-01',observed_at='2026-09-29T10:00:00Z',as_of='2026-09-29T10:00:00Z',revision=revision,amount_micros=-125000,currency='USD',cost_type='actual',payer='automaticai',environment='production',category='telephony',allocation='direct',coverage='complete',coverage_reason='',description='Phone credit',source={'kind':'fixture','reference':'fixture://bill','evidence_sha256':'a'*64})
    data['cost_key'] = b.digest(b.canonical([data[k] for k in ('client_id','project_id','provider','account_id','resource_id','period_start','period_end','currency','cost_type')]))
    identifier = str(uuid.uuid4())
    return dict(specversion='1.0',id=identifier,source='urn:fixture',type='bloodbank.billing.cost.observed',subject=b.SUBJECT,kind='event',domain='billing',producer='fixture',service='fixture',time=data['observed_at'],correlationid=identifier,causationid=None,ordering_key=data['cost_key'],actor={'type':'service','agent_id':'fixture'},schemaref='bloodbank.billing.cost.observed.v1',dataschema='apicurio://holyfields/bloodbank.billing.cost.observed/versions/1',data=data)


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'archive.sqlite'
        self.archive = b.Archive(self.path)
        self.scope = dict(client_id='one',project_id='one',ingress_token='fixture-credential-'+'x'*32,portal_token='fixture-projector-'+'x'*32,portal_url='https://portal.example/api/cost-observations')

    def tearDown(self):
        self.temp.cleanup()

    async def test_contract_credit_timestamps_scope_and_conflicts(self):
        value = event()
        b.validate(value,self.scope)
        self.archive.save(value)
        self.archive.save(value)
        self.assertEqual(self.archive.health()['archived'],1)
        changed = copy.deepcopy(value)
        changed['data']['amount_micros']=123
        with self.assertRaises(ValueError): self.archive.save(changed)
        with self.assertRaises(ValueError): self.archive.save(event())
        with self.assertRaises(PermissionError): b.validate(event('two'),self.scope)
        with self.assertRaises(PermissionError): b.authenticate('Bearer wrong', [self.scope])
        self.assertEqual(b.authenticate('Bearer '+self.scope['ingress_token'], [self.scope])['project_id'],'one')
        value['data']['as_of']='2026-09-29T11:00:00+02:00'
        b.validate(value)
        value['data']['as_of']='2026-09-29T09:00:00-02:00'
        with self.assertRaises(ValueError): b.validate(value)

    async def test_puback_required_archive_survives_and_original_replay(self):
        calls=[]
        async def fail(*args, **kwargs): raise ConnectionError()
        bridge=b.Bridge(self.archive,[self.scope],SimpleNamespace(publish=fail))
        value=event()
        with self.assertRaises(ConnectionError): await bridge.publish(value)
        self.assertEqual(self.archive.health()['pending_publish'],1)
        self.assertFalse(bridge.health()['ready'])
        self.assertEqual(self.archive.health()['errors'],1)
        reopened=b.Archive(self.path)
        async def publish(subject, body, **kwargs):
            calls.append((subject,json.loads(body),kwargs))
            return SimpleNamespace(stream='BLOODBANK_EVENTS',seq=7)
        bridge=b.Bridge(reopened,[self.scope],SimpleNamespace(publish=publish))
        receipt=await bridge.publish(reopened.events()[0])
        self.assertEqual(receipt['event_id'],value['id'])
        self.assertEqual(calls[0][2]['headers']['Nats-Msg-Id'],value['id'])
        self.assertEqual(reopened.health()['pending_publish'],0)
        self.assertTrue(bridge.health()['ready'])
        # Published originals remain after ACK for explicit replay beyond retention.
        self.assertEqual(reopened.events('one','2026-09'),[value])

    async def test_projection_ack_follows_commit_and_failed_projection_naks(self):
        calls=[]
        value=event()
        async def ack_sync(**kwargs): calls.append('ack')
        async def nak(**kwargs): calls.append('nak')
        message=SimpleNamespace(data=b.canonical(value).encode(),ack_sync=ack_sync,nak=nak)
        bridge=b.Bridge(self.archive,[self.scope],None)
        with patch.object(b,'project',side_effect=ConnectionError): await bridge.consume(message)
        self.assertEqual(calls,['nak'])
        self.assertEqual(self.archive.health()['pending_projection'],1)
        with patch.object(b,'project',side_effect=lambda *args:calls.append('committed')): await bridge.consume(message)
        self.assertEqual(calls,['nak','committed','ack'])
        self.assertEqual(self.archive.health()['pending_projection'],0)
        self.assertEqual(self.archive.health()['errors'],0)

    async def test_health_refuses_disconnected_broker_and_overdue_backlog_then_recovers(self):
        connected = False
        bridge = b.Bridge(self.archive, [self.scope], None, broker_connected=lambda: connected)
        self.assertFalse(bridge.health()['ready'])
        connected = True
        self.assertTrue(bridge.health()['ready'])
        value = event()
        with patch.object(b.time, 'time', return_value=100):
            self.archive.save(value)
        with patch.object(b.time, 'time', return_value=300):
            self.assertFalse(bridge.health()['ready'])
            self.assertEqual(bridge.health()['oldest_pending_seconds'], 200)
        self.archive.mark(value['id'], published=1, projected=1, error='')
        self.assertTrue(bridge.health()['ready'])

    async def test_complete_coverage_with_unknown_money_is_invalid(self):
        value = event()
        value['data']['amount_micros'] = None
        with self.assertRaises(Exception):
            b.validate(value)

    async def test_real_projector_refuses_foreign_receipt_and_accepts_redelivery(self):
        value = event()
        calls, requests = [], []
        async def ack_sync(**kw): calls.append('ack')
        async def nak(**kw): calls.append('nak')
        message = SimpleNamespace(data=b.canonical(value).encode(), ack_sync=ack_sync, nak=nak)
        bridge = b.Bridge(self.archive, [self.scope], None)
        receipts = [{'success': True, 'event_id': str(uuid.uuid4())}, {'success': True, 'event_id': value['id']}]
        def open_request(request, timeout):
            requests.append(request)
            response = io.BytesIO(b.canonical(receipts.pop(0)).encode())
            response.status = 200
            self.assertEqual(timeout, 30)
            return response
        with patch.object(b.urllib.request, 'build_opener', return_value=SimpleNamespace(open=open_request)):
            await bridge.consume(message)
            self.assertEqual(calls, ['nak'])
            self.assertEqual(self.archive.health()['pending_projection'], 1)
            self.assertEqual(self.archive.health()['errors'], 1)
            await bridge.consume(message)
        self.assertEqual(calls, ['nak', 'ack'])
        self.assertEqual(self.archive.health()['pending_projection'], 0)
        self.assertEqual(self.archive.health()['errors'], 0)
        self.assertEqual([json.loads(request.data)['id'] for request in requests], [value['id'], value['id']])
        self.assertTrue(all(request.full_url == self.scope['portal_url'] for request in requests))

    async def test_loop_fetches_only_work_that_can_start_before_its_ack_deadline(self):
        value = event()
        actions = []
        async def ack_sync(**kw): actions.append('ack')
        async def nak(**kw): actions.append('nak')
        message = SimpleNamespace(data=b.canonical(value).encode(), ack_sync=ack_sync, nak=nak)
        async def fetch(batch, **kw):
            actions.append(('fetch', batch))
            return [message]
        bridge = b.Bridge(self.archive, [self.scope], None)
        with patch.object(b, 'project'):
            await bridge.consume_next(SimpleNamespace(fetch=fetch))
        self.assertEqual(actions, [('fetch', 1), 'ack'])
        self.assertIn('await bridge.consume_next(sub)', inspect.getsource(b.run))

    async def test_poison_is_durable_private_terminal_evidence_and_unhealthy_after_restart(self):
        bad = event()
        bad['data']['amount_micros'] = 'invalid schema money'
        conflicting = event()
        self.archive.save(conflicting)
        conflicting['data']['description'] = 'conflicting original identity'
        for body in (b'{broken json', b.canonical(bad).encode(), b.canonical(event('foreign')).encode(), b.canonical(conflicting).encode()):
            with self.subTest(body=body[:20]):
                actions = []
                async def term(): actions.append('term')
                async def nak(**kw): actions.append('nak')
                async def ack_sync(**kw): actions.append('ack')
                message = SimpleNamespace(data=body, term=term, nak=nak, ack_sync=ack_sync,
                    metadata=SimpleNamespace(stream='BLOODBANK_EVENTS', consumer='costs', sequence=SimpleNamespace(stream=4, consumer=2)))
                bridge = b.Bridge(self.archive, [self.scope], None)
                with patch.object(b, 'project') as project:
                    await bridge.consume(message)
                    project.assert_not_called()
                self.assertEqual(actions, ['term'])
                reopened = b.Archive(self.path)
                with reopened.connect() as db:
                    row = db.execute('SELECT body,error,metadata,disposition FROM quarantine WHERE sha256=?', (b.hashlib.sha256(body).hexdigest(),)).fetchone()
                    self.assertEqual(row[0], body)
                    self.assertEqual(json.loads(row[2])['stream'], 'BLOODBANK_EVENTS')
                    self.assertEqual(row[3], 'terminal')
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute('UPDATE quarantine SET error=?', ('removed',))
                self.assertFalse(b.Bridge(reopened, [self.scope], None).health()['ready'])
        self.assertEqual(self.archive.health()['invalid_messages'], 4)

    async def test_quarantine_write_failure_never_terminates_or_acknowledges_message(self):
        actions = []
        async def term(): actions.append('term')
        message = SimpleNamespace(data=b'broken', term=term)
        with patch.object(self.archive, 'quarantine', side_effect=OSError('storage unavailable')):
            with self.assertRaises(OSError):
                await b.Bridge(self.archive, [self.scope], None).consume(message)
        self.assertEqual(actions, [])

    async def test_mapping_only_compose_configuration_authenticates_and_projects_both_clients(self):
        second = {**self.scope, 'client_id': 'two', 'project_id': 'two', 'ingress_token': 'second-ingress-' + 'y'*32, 'portal_token': 'second-projector-' + 'y'*32}
        mapping = [self.scope, second]
        with patch.dict(os.environ, {'COST_SCOPES_JSON': json.dumps(mapping)}, clear=True):
            configured = b.scopes_from_env()
            config = json.loads(subprocess.check_output(['docker', 'compose', '-f', str(Path(__file__).with_name('compose.yml')), 'config', '--format', 'json'], text=True, env={**os.environ, 'PATH': '/usr/local/bin:/usr/bin:/bin', 'NATS_URL': 'nats://fixture:4222'}))
        self.assertEqual(json.loads(config['services']['cost-bridge']['environment']['COST_SCOPES_JSON']), mapping)
        bridge = b.Bridge(self.archive, configured, None)
        receipts = []
        def request_response(request, timeout):
            value = json.loads(request.data)
            scope = next(s for s in mapping if s['project_id'] == value['data']['project_id'])
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + scope['portal_token'])
            receipts.append(value['data']['project_id'])
            response = io.BytesIO(b.canonical({'success': True, 'event_id': value['id']}).encode())
            response.status = 200
            return response
        for scope in configured:
            self.assertEqual(b.authenticate('Bearer ' + scope['ingress_token'], configured), scope)
            value = event(scope['project_id'])
            b.validate(value, scope)
            other = next(s for s in configured if s is not scope)
            with self.assertRaises(PermissionError): b.validate(value, other)
            ack = []
            async def ack_sync(**kw): ack.append(True)
            async def nak(**kw): self.fail('valid two-client projection must commit')
            message = SimpleNamespace(data=b.canonical(value).encode(), ack_sync=ack_sync, nak=nak)
            with patch.object(b.urllib.request, 'build_opener', return_value=SimpleNamespace(open=request_response)):
                await bridge.consume(message)
            self.assertEqual(ack, [True])
        self.assertEqual(receipts, ['one', 'two'])
        self.assertEqual(self.archive.health()['pending_projection'], 0)


if __name__=='__main__': unittest.main()
