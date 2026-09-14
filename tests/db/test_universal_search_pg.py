"""Run only against an explicitly disposable PostgreSQL database."""
import asyncio
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('ALLOW_REAL_PG') != '1', reason='requires disposable PostgreSQL')


@pytest.fixture(scope='module', autouse=True)
def schema():
    from app.db import base
    from app.db.database import init_database
    if base._pg_pool is None:
        base.init_pool(min_conn=1, max_conn=4)
    init_database()


def test_searches_messages_written_by_native_store():
    from app.db import sessions, profiles, base
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    token = uuid4().hex
    profile = 'search-profile-' + token
    sid = 'search-session-' + token
    store_id = 'ses_' + token
    profiles.create_profile(profile, 'Search test', '', {})
    sessions.create_session(sid, profile, title='Unrelated title')
    sessions.update_session(sid, sdk_session_id=store_id)

    async def produce():
        store = PgFluidStore()
        await store.create_session(store_id)
        await store.append(store_id, SessionNode('u', None, Message('user', [Text('userneedle' + token)])))
        await store.append(store_id, SessionNode('a', 'u', Message('assistant', [Text('assistantneedle' + token + ' 50%_complete "quoted"')], provider_meta={'hidden': 'metaneedle' + token})))
        await store.append(store_id, SessionNode('s', 'a', Message('system', [Text('systemneedle' + token)])))
    try:
        asyncio.run(produce())
        sessions.add_session_message(sid, 'user', 'oldpromptneedle' + token)
        sessions.add_session_message(sid, 'assistant', 'oldreplyneedle' + token)
        sessions.add_session_message(sid, 'system', 'oldsystemneedle' + token)
        for query in ['userneedle' + token, 'assistantneedle' + token, '50%_complete', '"quoted"', 'oldpromptneedle' + token, 'oldreplyneedle' + token]:
            assert sid in [row['id'] for row in sessions.search_sessions(query)]
            rows, count = sessions.search_sessions_paginated(query)
            assert sid in [row['id'] for row in rows]
            assert count >= 1
            assert rows[0]['match_type'] == 'content'
        for query in ['systemneedle' + token, 'oldsystemneedle' + token, 'metaneedle' + token, '50Xcomplete']:
            assert sid not in [row['id'] for row in sessions.search_sessions(query)]
        rows, count = sessions.search_sessions_paginated('userneedle' + token, profile_id='another-profile')
        assert rows == [] and count == 0
        sessions.update_session(sid, status='archived')
        assert sessions.search_sessions('userneedle' + token) == []
    finally:
        sessions.delete_session(sid)
        with base.get_db() as conn:
            conn.cursor().execute('DELETE FROM fluid_sessions WHERE session_id = %s', (store_id,))
            conn.cursor().execute('DELETE FROM agents WHERE id = %s', (profile,))

def test_note_search_title_body_and_literal_wildcards():
    from app.db import notes
    note_id = 'search-note-' + uuid4().hex
    notes.create_note(note_id, 'Orchid Title', 'Quartz body 50%_complete')
    try:
        for query in ['orchid', 'quartz', '50%_complete']:
            assert note_id in [row['id'] for row in notes.get_all_notes(search=query)]
        assert note_id not in [row['id'] for row in notes.get_all_notes(search='50Xcomplete')]
    finally:
        notes.delete_note(note_id)
