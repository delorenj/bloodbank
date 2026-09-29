import asyncio
import copy
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import tempfile
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


if __name__=='__main__': unittest.main()
