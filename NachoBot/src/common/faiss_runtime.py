"""Bound native parallelism for single-vector retrieval calls."""

from contextlib import contextmanager
from threading import RLock


_query_lock = RLock()


@contextmanager
def single_query_threads(faiss_module):
    # Apply inside the query worker, and serialize temporary limit changes:
    # Windows VCOMP can expose shared state that another worker could restore
    # while a query is running. Nested calls reuse this process-local lock.
    with _query_lock:
        previous = faiss_module.omp_get_max_threads()
        changed = previous != 1
        if changed:
            faiss_module.omp_set_num_threads(1)
        try:
            yield
        finally:
            if changed:
                faiss_module.omp_set_num_threads(previous)
