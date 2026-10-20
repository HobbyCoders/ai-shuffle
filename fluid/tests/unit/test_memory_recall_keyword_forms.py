"""Keyword recall keeps forms and non-space scripts without short collisions."""
import pytest
from fluid.codemode.relevance import recall_terms
from fluid.codemode.skills import SkillStore


@pytest.mark.parametrize("query,text", [
    ("worker", "The workers run on a bounded pool."),
    ("deploy", "Deployment uses a staged pipeline."),
    ("skill", "The SkillStore saves verified helpers."),
    ("test", "Run tests with a local interpreter."),
    ("テスト", "日本語のテスト設定を保持する。"),
    ("测试", "项目测试配置保存在本地。"),
    ("store", "FactStore retains memory revisions."),
    ("hub", "GitHub hosts this repository."),
    ("lab", "HomeLab has a dedicated runner."),
    ("PC", "GamingPC has a dedicated runner."),
    ("pytest", "日本語でpytestを実行する。"),
    ("ทดสอบ", "การทดสอบภายในระบบ"),
    ("ທົດສອບ", "ການທົດສອບລະບົບ"),
    ("စမ်းသပ်", "စနစ်စမ်းသပ်ခြင်း"),
    ("សាកល្បង", "ការសាកល្បងប្រព័ន្ធ"),
    ("C++", "The native helper is written in C++."),
    ("C#", "The service is written in C#."),
    ("ทุก", "ทดสอบทุกวัน"),
    ("วัน", "ทดสอบทุกวัน"),
    ("ທຸກ", "ທົດສອບທຸກມື້"),
    ("ថ្ងៃ", "សាកល្បងរាល់ថ្ងៃ"),
    ("រាល់", "សាកល្បងរាល់ថ្ងៃ"),
    ("တိုင်း", "စမ်းသပ်မှုနေ့တိုင်း"),
    ("pytest ทุก", "pytest ทำงานทุกวัน"),
])
def test_keyword_forms_and_cjk_remain_findable(tmp_path, query, text):
    store = SkillStore(tmp_path)
    expected = store.save_memory(text)
    store.save_memory("Unrelated platform convention.")
    assert [e.id for e, _ in store.search_memories(query)] == [expected.id]


def test_tool_stopwords_and_all_filtered_queries_have_literal_fallback(tmp_path):
    store = SkillStore(tmp_path)
    go = store.save_memory("Go compiler builds this service.")
    make = store.save_memory("The helper is built with make.")
    assert recall_terms("go make") == frozenset({"go", "make"})
    assert {e.id for e, _ in store.search_memories("go make")} == {go.id, make.id}
    assert any(e.id == go.id for e, _ in store.search_memories("Go build"))
    assert any(e.id == make.id for e, _ in store.search_memories("make test"))
    assert recall_terms("the with") == frozenset({"the", "with"})


def test_contractions_do_not_add_single_letter_neighbors(tmp_path):
    store = SkillStore(tmp_path)
    expected = store.save_memory("GamingPC has a dedicated runner.")
    store.save_memory("The worker can't access that host.")
    assert recall_terms("GamingPC's") == frozenset({"gamingpc"})
    assert "t" not in recall_terms("can't find runner")
    assert [e.id for e, _ in store.search_memories("GamingPC's")] == [expected.id]


def test_ipv4_is_one_exact_keyword_not_shared_components(tmp_path):
    store = SkillStore(tmp_path)
    expected = store.save_memory("The fixture endpoint is 10.0.0.251.")
    store.save_memory("The other fixture endpoint is 10.0.0.1.")
    assert recall_terms("10.0.0.251") == frozenset({"10.0.0.251"})
    assert [e.id for e, _ in store.search_memories("10.0.0.251")] == [expected.id]


@pytest.mark.parametrize("query,text", [
    ("UI", "日本語でbuildを実行する。"),
    ("don't", "Donation settings are separate."),
    ("C++", "The C helper uses a compiler."),
    ("C#", "The C helper uses a compiler."),
])
def test_short_and_contraction_literals_avoid_new_neighbors(tmp_path, query, text):
    store = SkillStore(tmp_path)
    store.save_memory(text)
    assert not store.search_memories(query)
    assert recall_terms("don't") == frozenset({"dont"})
