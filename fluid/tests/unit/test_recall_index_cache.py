"""A bounded per-text cache changes CPU work, never lexical scores."""
import pytest
from fluid.codemode import relevance as matcher
from fluid.codemode.skills import SkillStore


@pytest.fixture(autouse=True)
def empty_index():
    matcher._recall_index.cache_clear()
    yield
    matcher._recall_index.cache_clear()


def test_cached_and_uncached_scores_are_identical(monkeypatch):
    texts = (
        "Workers deploy with FactStore, GitHub and GamingPC; UI is separate from build.",
        "C++ and C# clients connect to 10.0.0.251, not 10.0.0.1.",
        "日本語でpytestを実行する。 टेबल पर किताब है। यह सर्वर तैयार है।",
        "ทดสอบทุกวัน សាកល្បងរាល់ថ្ងៃ ທົດສອບທຸກມື້ စမ်းသပ်မှုနေ့တိုင်း",
        "می‌خواهم کتاب بخوانم. ශ්‍රී ලංකා.",
        "Don't confuse Donation with a negative instruction. A café serves lunch.",
    )
    queries = ("worker", "deploy", "store", "hub", "PC", "UI", "C++", "C#", "10.0.0.251",
               "pytest", "तैयार", "ทุก", "រាល់", "ທຸກ", "တိုင်း", "می‌خواهم", "ශ්‍රී", "cafe\u0301", "don't")
    terms = [matcher.recall_terms(query) for query in queries]
    def scores():
        return [matcher.recall_relevance(text, query) for query in terms for text in texts]
    cached = matcher._recall_index
    actual = scores()
    assert scores() == actual and cached.cache_info().hits > 0
    with monkeypatch.context() as patch:
        patch.setattr(matcher, "_recall_index", cached.__wrapped__)
        assert scores() == actual


def test_cache_is_bounded_immutable_and_keyed_by_full_text():
    cached = matcher._recall_index
    folded, words, unsegmented = cached("FactStore 日本語")
    assert isinstance(folded, str) and isinstance(words, frozenset) and isinstance(unsegmented, tuple)
    assert matcher.recall_relevance("alpha service", frozenset({"alpha"})) == 1
    assert matcher.recall_relevance("beta service", frozenset({"alpha"})) == 0
    for i in range(4097):
        cached(f"Unique fixture {i}")
    info = cached.cache_info()
    assert info.maxsize == 4096 and info.currsize == 4096


@pytest.mark.parametrize("query,expected,neighbor", [
    ("می‌خواهم", "من می‌خواهم کتاب بخوانم.", "او می‌رود به خانه"),
    ("ශ්‍රී", "ශ්‍රී ලංකා.", "ශ් රී are separate fixture fragments."),
])
def test_join_controls_stay_inside_words_without_prefix_neighbors(tmp_path, query, expected, neighbor):
    store = SkillStore(tmp_path)
    wanted = store.save_memory(expected)
    store.save_memory(neighbor)
    assert matcher.recall_terms(query) == frozenset({query.casefold()})
    assert [entry.id for entry, _ in store.search_memories(query)] == [wanted.id]
