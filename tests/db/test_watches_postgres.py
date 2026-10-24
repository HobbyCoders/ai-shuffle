"""Watch cursor, budget, leases and populated upgrade on disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import pytest

from tests.db.test_bounded_maintenance_postgres import cluster, pg  # noqa: F401
from tests.db.test_watch_migration import legacy_schema


@pytest.fixture
def watchpg(pg,monkeypatch):
    from app.db import watches as store,watch_migration
    # Production helpers intentionally inspect public. This fixture gives each
    # test a private search_path; use identical real catalog queries there.
    def column_exists(c,table,column):
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() "
                  "AND table_name=%s AND column_name=%s",(table,column))
        return c.fetchone() is not None
    def table_exists(c,table):
        c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=%s",(table,))
        return c.fetchone() is not None
    from app.db import base
    monkeypatch.setattr(base,'column_exists',column_exists)
    monkeypatch.setattr(watch_migration,'table_exists',table_exists)
    return pg


def test_real_watch_transactions_and_reopen(watchpg,monkeypatch):
    from app.db import watches as store
    sql,_,_,_,_,connect = watchpg
    monkeypatch.setattr(store,'get_db',lambda:connect(False))
    sql('CREATE TABLE agents(id TEXT PRIMARY KEY)')
    sql('CREATE TABLE sessions(id TEXT PRIMARY KEY)')
    sql("INSERT INTO agents VALUES ('default')")
    with connect(False) as conn:
        store.create_schema(conn.cursor())
    wid = store.create(owner='admin',creating_session_id=None,worker_id='worker',agent_id='default',
        description='Reminder',source={'kind':'schedule','interval_seconds':60},filter={},action='alert',
        permissions={'tools':[],'read_only':True},daily_budget_usd=1)
    watch = store.get(wid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _:store.checkpoint(watch,[('one',{})],{'next':1}),range(8)))==1
    assert store.get(wid)['cursor']=={'next':1}
    event = store.events(wid)[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _:store.reserve(wid,event['id'],1),range(8)))==1
    assert store.claim_delivery(event['id'],'first')
    assert not store.claim_delivery(event['id'],'second')
    store.release_delivery(event['id'],'first')
    assert store.claim_delivery(event['id'],'second')
    store.settle(event['id'],0.9)
    store.append_events(store.get(wid),[('two',{})])
    assert not store.reserve(wid,store.events(wid,('pending',))[0]['id'],0.25)
    assert store.get(wid)['status']=='paused'


def test_populated_email_upgrade_real_postgres(watchpg,monkeypatch):
    from app.db import watches as store,watch_migration
    sql,_,_,_,_,connect = watchpg
    monkeypatch.setattr(store,'get_db',lambda:connect(False))
    sql('CREATE TABLE agents(id TEXT PRIMARY KEY)')
    sql('CREATE TABLE sessions(id TEXT PRIMARY KEY)')
    sql("INSERT INTO agents VALUES ('default')")
    sql('CREATE TABLE connectors(id TEXT PRIMARY KEY,type TEXT,name TEXT,runner_id TEXT)')
    sql("INSERT INTO connectors VALUES ('mail','gmail','Inbox','worker')")
    sql('CREATE TABLE assistant_state(id TEXT PRIMARY KEY,agent_id TEXT)')
    sql("INSERT INTO assistant_state VALUES ('worker','default')")
    with connect(False) as conn:
        legacy_schema(conn.cursor())
    sql("INSERT INTO assistant_sources VALUES ('worker','mail',TRUE,'{\"seen_ids\":[\"before\"]}',NOW())")
    sql("INSERT INTO assistant_events VALUES ('e','worker','mail','new_email','fingerprint',"
        "'{\"message_id\":\"pending\"}','pending',NULL,NOW())")
    with connect(False) as conn:
        assert watch_migration.migrate(conn.cursor())==['email_research_to_watches']
    watch = store.list_watches()[0]
    assert watch['cursor']['legacy_seen_ids']==['before']
    assert store.events(watch['id'])[0]['payload']['message_id']=='pending'
    with connect(False) as conn:
        assert watch_migration.migrate(conn.cursor())==[]
    assert sql("SELECT to_regclass('assistant_sources') AS name")[0]['name'] is None
