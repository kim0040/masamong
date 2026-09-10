from utils.rag_policy import (
    bm25_runtime_enabled,
    should_construct_bm25_manager,
    should_run_bm25_search,
)


def test_bm25_stays_off_even_if_a_path_is_present():
    assert bm25_runtime_enabled() is False
    assert should_construct_bm25_manager("/var/lib/masamong/masamo/bm25.db") is False
    assert should_run_bm25_search(manager=object(), enabled=True) is False
    assert should_run_bm25_search(manager=None, enabled=None) is False
