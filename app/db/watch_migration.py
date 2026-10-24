"""One-way retirement of the email research loop, in the startup transaction."""
from __future__ import annotations

import json
from datetime import timedelta

from app.db.base import row_to_dict, table_exists
from app.db import watches as store


def _json(value, default=None):
    if isinstance(value,str):
        return json.loads(value)
    return value if value is not None else default


def migrate(cursor):
    if not table_exists(cursor,'assistant_sources'):
        return []
    store.create_schema(cursor)
    cursor.execute('SELECT s.*,c.type,c.name,c.runner_id FROM assistant_sources s JOIN connectors c ON c.id=s.connector_id ORDER BY s.assistant_id,s.connector_id')
    selections = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT id FROM agents WHERE id='default'")
    normal = cursor.fetchone()
    fallback = normal['id'] if normal else None
    watches = {}
    for source in selections:
        if not source['enabled'] or source['type']!='gmail':
            continue
        cursor.execute('SELECT * FROM assistant_state WHERE id=%s',(source['assistant_id'],))
        state = dict(cursor.fetchone() or {})
        agent = fallback or state.get('agent_id')
        if not agent:
            continue
        wid = 'migrated-email-'+store.event_id(source['assistant_id'],source['connector_id'])
        bookmark = _json(source.get('watermark'),{}) or {}
        # Old unread cursors are carried, not mistaken for Gmail history ids.
        bookmark = {'legacy_seen_ids':bookmark.get('seen_ids',[])}
        if source.get('last_observed_at'):
            bookmark['legacy_after'] = int(store.at(source['last_observed_at']).timestamp())-1
        cursor.execute("""INSERT INTO watches
            (id,owner,worker_id,agent_id,description,source,filter,action,permissions,cursor,daily_budget_usd,status,reason)
            VALUES (%s,'admin',%s,%s,%s,%s,'{}','investigate',%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (wid,source['runner_id'] or source['assistant_id'],agent,'Email: '+source['name'],
             store.encode(dict(kind='connector',connector_id=source['connector_id'])),
             store.encode(dict(read_only=True,tools=[])),store.encode(bookmark),
             5.0/max(1,sum(s['enabled'] and s['type']=='gmail' and s['assistant_id']==source['assistant_id'] for s in selections)),
             'paused' if state.get('paused') or not state.get('enabled',True) else 'active',
             'Email research was paused or disabled before migration' if state.get('paused') or not state.get('enabled',True) else None))
        watches[(source['assistant_id'],source['connector_id'])] = wid
    cursor.execute('SELECT * FROM assistant_events ORDER BY observed_at,id')
    events = [dict(r) for r in cursor.fetchall()]
    grouped = {}
    for event in events:
        wid = watches.get((event['assistant_id'],event['source_id']))
        if not wid:
            continue
        payload = _json(event['payload'],{})
        if isinstance(payload.get('data'),str):
            try:
                payload = _json(payload['data'],{})
            except ValueError:
                pass
        payload = dict(payload,event_type=event['event_type'])
        key = event['event_type']+':'+str(payload.get('message_id') or event['fingerprint'])
        # Previously dispatched batches are adopted below as ONE turn, not one
        # new turn per observation. These rows retain their dedupe fingerprints.
        status = 'pending' if event['status']=='pending' and not event.get('run_id') else 'delivered'
        cursor.execute("""INSERT INTO watch_events (id,watch_id,event_key,payload,status,cost_usd,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(watch_id,event_key) DO NOTHING""",
            (store.event_id(wid,key),wid,key,store.encode(payload),status,0 if status=='delivered' else None,event['observed_at']))
        if event.get('run_id'):
            grouped.setdefault(event['run_id'],[]).append((wid,payload))
    cursor.execute('SELECT * FROM assistant_runs ORDER BY created_at,id')
    for raw in cursor.fetchall():
        run = dict(raw)
        batch = grouped.get(run['id'])
        if not batch:
            continue
        # A legacy batch can include several opted-in sources. It stays one
        # bounded, context-only delivery, attached to its first email watch.
        wid = batch[0][0]
        key = 'legacy-run:'+run['id']
        eid = store.event_id(wid,key)
        cursor.execute('SELECT * FROM turns WHERE session_id=%s AND command_id=%s ORDER BY created_at LIMIT 1',
                       (run.get('session_id'),run['id']))
        turn = row_to_dict(cursor.fetchone())
        terminal = run['status'] not in ('admitted','submitting','running','cancelling') or (turn and turn['state'] in ('completed','aborted','parked'))
        status = 'delivered' if terminal else 'running' if turn else 'pending'
        cost = float(run.get('cost_usd') or 0)
        deadline = run.get('deadline_at') or store.now()+timedelta(seconds=180)
        cursor.execute("""INSERT INTO watch_events
            (id,watch_id,event_key,payload,status,session_id,turn_id,command_id,route,reserved_usd,cost_usd,deadline_at,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s) ON CONFLICT(watch_id,event_key) DO NOTHING""",
            (eid,wid,key,store.encode({'legacy_batch':[p for _,p in batch]}),status,
             turn['session_id'] if turn else None,turn['id'] if turn else None,run['id'],
             'investigate' if turn else None,cost if terminal else None,deadline,run['created_at']))
        if cursor.rowcount and terminal:
            cursor.execute('UPDATE watches SET cost_usd=cost_usd+%s,last_fired_at=%s WHERE id=%s',(cost,run['created_at'],wid))
        if turn and not terminal:
            cursor.execute('SELECT * FROM watches WHERE id=%s',(wid,))
            watch = store.parse(cursor.fetchone())
            cursor.execute('SELECT * FROM watch_events WHERE id=%s',(eid,))
            event = store.parse(cursor.fetchone())
            from app.core.watches import metadata
            meta = metadata(watch,event)
            overrides,payload = _json(turn['overrides'],{}),_json(turn['payload'],{})
            overrides.update(_assistant_proactive=meta,max_turns=3)
            payload['_assistant_proactive'] = meta
            cursor.execute('UPDATE turns SET overrides=%s,payload=%s WHERE id=%s',
                           (store.encode(overrides),store.encode(payload),turn['id']))
    # The remote jobs/costs and owner introduction/account are still live users.
    # Their historical table names deliberately stay. Peer source opt-ins are
    # ignored after this local migration; they never re-create background work.
    cursor.execute('DROP TABLE assistant_events')
    cursor.execute('DROP TABLE assistant_runs')
    cursor.execute('DROP TABLE assistant_sources')
    return ['email_research_to_watches']
