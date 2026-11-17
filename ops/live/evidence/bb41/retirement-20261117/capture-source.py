"""Capture BB-41 post-retirement proof read-only; no prompts, tool args, or event payloads persisted."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import urllib.request

HOME = Path.home()
SCRATCH = HOME/'.hermes/profiles/bloodbank-pm/cache/scratch'
sys.path.insert(0, str(SCRATCH))
# Standalone decoder from the initial evidence pass, copied into the capture below.

def ro(path):
    c = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c

def digest(raw):
    return hashlib.sha256(raw.encode() if isinstance(raw,str) else raw).hexdigest()

def unflatten(raw):
    values=json.loads(raw); memo={}
    def item(i):
        if i in memo: return memo[i]
        v=values[i]
        if isinstance(v,dict):
            result={};memo[i]=result;result.update({k:ref(x) for k,x in v.items()})
        elif isinstance(v,list):
            result=[];memo[i]=result;result.extend(ref(x) for x in v)
        else:
            result=v;memo[i]=result
        return result
    def ref(v): return item(int(v)) if isinstance(v,str) and v.isdigit() else v
    return item(0)

def iso(value): return datetime.fromisoformat(value.replace('Z','+00:00')).replace(tzinfo=timezone.utc)
def now(): return datetime.now(timezone.utc).isoformat()
def save(path,data):
    with path.open('x') as f: json.dump(data,f,indent=2);f.write('\n')

def run(out):
    after=json.loads((out/'after.json').read_text())
    cutoff=after['captured_at']; epoch=iso(cutoff).timestamp()
    captured=now()
    result={'captured_at':captured,'post_retirement_cutoff':cutoff,'read_only':True,
            'sources':['n8n SQLite execution_entity + execution_data, mode=ro/query_only',
                       'Hermes gateway SQLite executions, mode=ro/query_only',
                       'Correlated profile SQLite sessions/messages, mode=ro/query_only',
                       'Krebs existing tick-journal.jsonl'],
            'sanitization':'Only execution metadata, node names/status/counts, allowlisted identifiers and content digests; no prompts, node code, credentials, event payloads, tool args or results.',
            'n8n_executions':[],'gateway':[],'agent_sessions':[]}
    commands={}
    lifecycle_event_ids=set()
    since=iso(cutoff).strftime('%Y-%m-%d %H:%M:%S.%f')
    with ro(HOME/'.n8n/database.sqlite') as db:
        rows=db.execute('SELECT e.id,e.workflowId,e.startedAt,e.stoppedAt,e.status,d.data FROM execution_entity e JOIN execution_data d ON d.executionId=e.id WHERE e.workflowId IN (?,?) AND e.startedAt>? ORDER BY e.id LIMIT 200',('6wAGA5pdrmHLyhs2','wWXgCZiiIBWaRRzE',since)).fetchall()
        for r in rows:
            run_data=unflatten(r['data']).get('resultData',{}).get('runData',{})
            nodes={}
            identifiers=[]
            for name,runs in run_data.items():
                nodes[name]=[{'status':v.get('executionStatus'),'execution_time_ms':v.get('executionTime'),'output_items_per_branch':[len(b) for b in v.get('data',{}).get('main',[])]} for v in runs]
                for v in runs:
                    for branch in v.get('data',{}).get('main',[]):
                        for x in branch:
                            j=x.get('json',{})
                            if name=='Invocation Lifecycle' and j.get('id'): lifecycle_event_ids.add(j['id'])
                            safe={k:j[k] for k in ('ticketKey','commandId','correlationid','agentId','operation','phase','reason','id','type') if k in j and (j[k] is None or isinstance(j[k],(str,int,float,bool)))}
                            if safe: identifiers.append({'node':name,'fields':safe})
                            if j.get('commandId'): commands[j['commandId']]={'origin':'post_retirement_n8n','execution_id':r['id'],'ticket':j.get('ticketKey')}
            result['n8n_executions'].append({k:r[k] for k in ('id','workflowId','startedAt','stoppedAt','status')}|{'source_data_sha256':digest(r['data']),'nodes':nodes,'identifiers':identifiers})
        result['legacy_executions_started_after_cutoff']=[dict(r) for r in db.execute('SELECT id,workflowId,startedAt,status FROM execution_entity WHERE workflowId IN (?,?) AND startedAt>? ORDER BY id LIMIT 200',('l0J53w0TsS5PwETk','8mmqdMwQYA28ZwUj',since))]
    journal=HOME/'.local/state/krebs/tick-journal.jsonl'
    journal_rows=[]
    for line in journal.read_text().splitlines():
        r=json.loads(line)
        # Keep only dispatch event identifiers; no board snapshots or prompt text.
        if r.get('action') not in ('dispatch.write','dispatch.publish'): continue
        if r.get('key')=='BB-41':
            journal_rows.append(r)
        elif any(isinstance(r.get(k),str) and r[k]>=cutoff[:19] for k in ('ts','at','timestamp','time')):
            journal_rows.append(r)
    result['krebs_dispatch_records']=journal_rows
    commands['fb758fac-bdb9-5b56-a2b8-1c83e23fcfd4']={'origin':'pre_retirement_BB41_dispatch','ticket':'BB-41','limit':'Post-cutoff tools corroborate continuing implementation, not a new post-retirement dispatch.'}
    with ro(HOME/'.hermes/bloodbank-hermes-gateway-state.sqlite3') as db:
        for event_id in lifecycle_event_ids:
            for r in db.execute('SELECT command_id,started_events,terminal_events FROM executions WHERE instr(started_events,?)>0 OR instr(terminal_events,?)>0',(event_id,event_id)):
                events=[e for field in ('started_events','terminal_events') for e in json.loads(r[field] or '[]') if e.get('id')==event_id]
                if events:
                    e=events[0]
                    commands.setdefault(r['command_id'],{'origin':'post_retirement_pickup_chip_event','matched_event_id':event_id,'event_observed_after_retirement':True,'ticket':(e.get('data',{}).get('context') or {}).get('ticket_key'),'limit':'Lifecycle event handled after retirement; command may have been created before retirement. Actual session tools are counted only after cutoff.'})
        for cid,origin in commands.items():
            r=db.execute('SELECT command_id,profile,state,outcome,created_at,updated_at,started_events,terminal_events FROM executions WHERE command_id=?',(cid,)).fetchone()
            if not r:
                result['gateway'].append({'command_id':cid,'found':False,**origin});continue
            entry={k:r[k] for k in ('command_id','profile','state','outcome','created_at','updated_at')}|origin
            events=[];threads=set();sessions=set()
            for field in ('started_events','terminal_events'):
                for e in json.loads(r[field] or '[]'):
                    d=e.get('data',{});ctx=d.get('context') or {}
                    if d.get('thread_id'): threads.add(d['thread_id'])
                    if d.get('session_id'): sessions.add(d['session_id'])
                    events.append({'id':e.get('id'),'type':e.get('type'),'time':e.get('time'),
                                   'thread_id':d.get('thread_id'),'session_id':d.get('session_id'),
                                   'context':{k:v for k,v in ctx.items() if k in ('ticket_key','dispatch_id','session_id','thread_id')}})
            entry['events']=events;result['gateway'].append(entry)
            profile_path=HOME/'.hermes/profiles'/r['profile']/'state.db'
            if not profile_path.exists():continue
            with ro(profile_path) as h:
                matches=[]
                for s in h.execute('SELECT id,source,session_key,thread_id,started_at,ended_at FROM sessions'):
                    if s['id'] in sessions or s['thread_id'] in threads or any(t and (t in (s['session_key'] or '') or t.split(':')[-1] in (s['session_key'] or '')) for t in threads):matches.append(s)
                for s in matches:
                    msgs=h.execute('SELECT id,role,tool_name,timestamp,content,tool_calls FROM messages WHERE session_id=? AND timestamp>=? ORDER BY id',(s['id'],epoch)).fetchall()
                    records=[{'message_id':m['id'],'tool_name':m['tool_name'],'timestamp':m['timestamp'],'at':datetime.fromtimestamp(m['timestamp'],timezone.utc).isoformat(),'content_sha256':digest(m['content'] or '')} for m in msgs if m['role']=='tool']
                    result['agent_sessions'].append(dict(s)|{'profile':r['profile'],'command_id':cid,**origin,'actual_post_retirement_tool_results':records,'tool_result_count':len(records),'assistant_tool_call_records':sum(bool(m['tool_calls']) for m in msgs if m['role']=='assistant')})
    result['limits']=['n8n node execution is not agent execution; pending commands remain pending.',
                      'An already-running BB-41 command is continuing execution, not proof of a newly dispatched post-retirement command.',
                      'This bounded observation does not prove continuous 24-hour availability.']
    save(out/'post-handler-evidence.json',result)
    api='http://127.0.0.1:8700/candybars/01a1d9de-d3cb-7d23-9a8b-2bc34e3bdac5'
    def get(url):
        with urllib.request.urlopen(url,timeout=10) as response:return json.load(response)
    manifest=get(api)
    runs=get(api+'/invocations?limit=5')
    candy={'captured_at':now(),'read_only':True,'manifest_get':api,'history_get':api+'/invocations?limit=5',
           'manifest':{k:manifest[k] for k in ('id','name','schedule','timezone','enabled','archived')},
           'invocations':[{k:r.get(k) for k in ('id','trigger','tickId','result','exitCode','startedAt','finishedAt')} for r in runs['invocations']],
           'seconds_after_immediate_after_snapshot':(iso(now())-iso(cutoff)).total_seconds()}
    save(out/'candybar-observation.json',candy)
    # Exercise LANDED parser with the real monitoring GET captured by the landed snapshot CLI.
    sys.path.insert(0,str(HOME/'code/33GOD/33god-platform/waybill'))
    from waybill.collectors import nats
    from waybill.model import ms
    later=json.loads((out/'later.json').read_text())
    consumers,streams=nats.parse_jsz(later['jsz'])
    n8n=[c for c in consumers if c['stream']=='BLOODBANK_EVENTS' and c['component']=='n8n']
    subjects=sorted({f for c in n8n for f in c['filter_subjects']}|{'bloodbank.evt.agent.invocation.completed','bloodbank.evt.repo.task.updated'})
    idle=nats.idle_consumers(consumers,subjects,ms(later['jsz']['now']))
    save(out/'waybill-live-parser.json',{'captured_at':now(),'method':'Landed parse_jsz + idle_consumers on later.json actual monitoring GET; subject inputs derived from live filters plus exact retired filters. Diagnostic parser survey, not a production end-to-end turn capture. No event was published.',
         'source_commit':'76956b57412bd8331368862c4c7477e6c27891d2','integrated_superproject_main':'52fa51a',
         'collector_source_sha256':digest(Path(nats.__file__).read_bytes()),'snapshot_file':'later.json','snapshot_sha256':digest((out/'later.json').read_bytes()),
         'monitor_get':'http://127.0.0.1:8222/jsz?streams=true&consumers=true&config=true','observed_at':later['jsz']['now'],
         'subjects':subjects,'n8n_consumers':n8n,'retired_durables_present':sorted({c['name'] for c in consumers}&nats.RETIRED_DELEGATION_DURABLES),
         'n8n_idle_candidates':[c for c in idle if c['component']=='n8n'],
         'limits':'No continuous 24-hour outage/absence claim; current n8n activation is separately captured in later.json.'})
    print(json.dumps({'n8n_executions':[(x['id'],x['status']) for x in result['n8n_executions']],
          'gateway':[{k:r.get(k) for k in ('command_id','state','profile','ticket')} for r in result['gateway']],
          'agent_session_tools':[{k:r.get(k) for k in ('command_id','profile','tool_result_count','assistant_tool_call_records','origin')} for r in result['agent_sessions']],
          'legacy_runs':result['legacy_executions_started_after_cutoff'],'seconds_after_retirement':candy['seconds_after_immediate_after_snapshot'],
          'n8n_count':len(n8n),'n8n_idle_candidates':[c for c in idle if c['component']=='n8n']},indent=2))

if __name__=='__main__':run(Path(sys.argv[1]))
