"""Single-query limits must cover each worker and preserve retrieval behavior."""

import ast
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import List, Tuple

import faiss
import numpy as np
import pytest

from src.common.faiss_runtime import single_query_threads


def method_from_source(relative, class_name, method_name):
    path = Path(__file__).resolve().parents[1] / relative
    tree = ast.parse(path.read_text(encoding="utf8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    scope = dict(np=np, faiss=faiss, List=List, Tuple=Tuple,
                 logger=logging.getLogger("faiss-test"), single_query_threads=single_query_threads)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
    return scope[method_name]


def test_restores_calling_thread_limit_after_exception_and_nested_context():
    previous = faiss.omp_get_max_threads()
    try:
        faiss.omp_set_num_threads(3)
        with pytest.raises(RuntimeError):
            with single_query_threads(faiss):
                assert faiss.omp_get_max_threads() == 1
                with single_query_threads(faiss):
                    assert faiss.omp_get_max_threads() == 1
                assert faiss.omp_get_max_threads() == 1
                raise RuntimeError("query failed")
        assert faiss.omp_get_max_threads() == 3
    finally:
        faiss.omp_set_num_threads(previous)


def test_each_worker_is_bounded_without_overlapping_limit_restoration():
    barrier = threading.Barrier(4)
    previous = faiss.omp_get_max_threads()

    def work(i):
        barrier.wait(timeout=5)
        with single_query_threads(faiss):
            return faiss.omp_get_max_threads()

    try:
        faiss.omp_set_num_threads(3)
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(work, range(4))) == [1, 1, 1, 1]
        assert faiss.omp_get_max_threads() == 3
    finally:
        faiss.omp_set_num_threads(previous)


def test_memorix_parallel_queries_preserve_ranking_scores_and_input(monkeypatch):
    search = method_from_source("src/A_memorix/core/storage/vector_store.py", "VectorStore", "search")
    rng = np.random.default_rng(612)
    data = rng.random((256, 32), dtype=np.float32)
    data /= np.linalg.norm(data, axis=1, keepdims=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(32))
    index.add_with_ids(data, np.arange(len(data), dtype=np.int64))
    store = SimpleNamespace(dimension=32, _is_trained=False, _fallback_index=index,
                            _lock=threading.RLock(), _flush_write_buffer_unlocked=lambda: None,
                            _deleted_ids=set(), _int_to_str_map={i: str(i) for i in range(len(data))})
    normalize = faiss.normalize_L2
    limits = []

    def observed_normalize(query):
        limits.append(faiss.omp_get_max_threads())
        return normalize(query)

    monkeypatch.setattr(faiss, "normalize_L2", observed_normalize)
    queries = rng.random((8, 32), dtype=np.float32)
    original = queries.copy()
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda q: search(store, q, 5), queries))
    np.testing.assert_array_equal(queries, original)
    assert limits == [1] * 8
    for query, (ids, scores) in zip(queries, answers):
        expected = data @ (query / np.linalg.norm(query))
        best = np.argsort(expected)[::-1][:5]
        assert ids == [str(i) for i in best]
        np.testing.assert_allclose(scores, expected[best], rtol=1e-5)
    with pytest.raises(ValueError, match="non-finite"):
        search(store, np.full(32, np.nan), 5)


def test_knowledge_query_limits_native_call_and_preserves_existing_scores():
    # Preserve this caller's existing score contract as part of the thread fix.
    search = method_from_source("src/chat/knowledge/embedding_store.py", "EmbeddingStore", "search_top_k")
    observed = []

    class Index:
        def search(self, query, count):
            observed.append(faiss.omp_get_max_threads())
            np.testing.assert_array_equal(query, [[3.0, 4.0]])
            return np.array([[0.7, 0.2]]), np.array([[1, 0]])

    store = SimpleNamespace(faiss_index=Index(), idx2hash={"0": "a", "1": "b"})
    previous = faiss.omp_get_max_threads()
    assert search(store, [3.0, 4.0], 2) == [("b", 0.7), ("a", 0.2)]
    assert observed == [1]
    assert faiss.omp_get_max_threads() == previous
