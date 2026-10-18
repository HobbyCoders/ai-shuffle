"""An unsent row is restored durably without touching any provider transcript."""
from unittest.mock import Mock
import pytest
from tests.api.test_compact_first import compact_chat, send
from tests.api.test_compact_first_delivery import query
from app.api import commands
from app.core import query_session_lifecycle
from app.db import database,session_events,turns

@pytest.fixture
def rewind_chat(compact_chat,monkeypatch):
    compact_chat.app.include_router(commands.router)
    transcript=Mock(side_effect=AssertionError("unsent restore must not inspect a model transcript"))
    monkeypatch.setattr(commands,'_get_checkpoints',transcript)
    monkeypatch.setattr(commands.jsonl_rewind_service,'truncate_to_checkpoint',transcript)
    return compact_chat

async def test_unsent_api_restores_only_row_and_uploads_idempotently_after_reload(rewind_chat):
    session_events.append_events('compact-chat',[{'type':'message.user','payload':{'content':'earlier sent'}}])
    pair=send(rewind_chat,{'then_send':query(upload_paths=['/uploads/proof.png'])}).json()
    turns.start_turn(pair['turn_id'])
    await query_session_lifecycle.enter_user_stop('compact-chat')
    unsent=next(e for e in session_events.get_events('compact-chat') if e['payload'].get('not_sent'))
    session_events.append_events('compact-chat',[{'type':'message.assistant','payload':{'content':'later real history'}}])
    url='/api/v1/commands/rewind/execute/compact-chat'
    for _ in range(2):
        result=rewind_chat.post(url,json={'target_not_sent_id':unsent['id']})
        assert result.status_code==200,result.text
        body=result.json()
        assert body['success'] and body['unsent_restored'] and not body['chat_rewound']
        assert body['restored_prompt']==query()['prompt']
        assert body['restored_upload_paths']==['/uploads/proof.png']
    assert [r['payload']['content'] for r in commands._load_message_events('compact-chat')]==['earlier sent','later real history']
    notes=[r for r in session_events.get_events('compact-chat') if r['payload'].get('subtype')=='unsent_restored']
    assert len(notes)==1
    assert not database.get_session_messages('compact-chat')
    assert turns.get_turn(pair['queued_turn_id'])['state']=='aborted'
    assert turns.pop_ready('compact-chat') is None


def test_unsent_api_fails_closed_for_sent_row_or_other_chat(rewind_chat):
    database.create_session('other','default')
    session_events.append_events('other',[{'type':'message.user','payload':{'content':'other','not_sent':True}}])
    session_events.append_events('compact-chat',[{'type':'message.user','payload':{'content':'real sent'}}])
    for target in (session_events.get_events('other')[0]['id'],session_events.get_events('compact-chat')[0]['id'],'missing'):
        result=rewind_chat.post('/api/v1/commands/rewind/execute/compact-chat',json={'target_not_sent_id':target})
        assert result.status_code==200
        assert not result.json()['success']
    assert all(r['event_type']=='message.user' for r in session_events.get_events('compact-chat'))

def test_normal_rewind_anchor_ignores_and_deletes_later_unsent_duplicate(rewind_chat,monkeypatch):
    from app.core import rewind_db_sync as sync
    database.add_session_message('compact-chat','user','same words')
    database.add_session_message('compact-chat','assistant','real reply')
    database.add_session_message('compact-chat','user','same words',metadata={'not_sent':True,'queued_turn_id':'stopped'})
    first=database.get_session_messages('compact-chat')[0]
    entries=[{'uuid':'u-first','type':'user','message':{'role':'user','content':'same words'}},
             {'uuid':'a-first','type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'real reply'}]}}]
    monkeypatch.setattr(sync,'_load_transcript_entries',lambda *a:entries)
    anchor=sync.resolve_rewind_anchor(session_id='compact-chat',sdk_session_id='sdk',working_dir='/unused',
                                     target_uuid='u-first')
    assert anchor==first['id']
    assert sync.apply_rewind_delete(session_id='compact-chat',anchor_id=anchor,include_response=False)==3
    assert database.get_session_messages('compact-chat')==[]

