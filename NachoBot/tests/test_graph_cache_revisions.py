"""Isolated regressions for graph-derived cache revision behavior."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _package(name: str, path: Path | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__package__ = name
    module.__path__ = [str(path)] if path is not None else []  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load isolated test module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_isolated_modules() -> tuple[type, type, type, Any]:
    """Load only the graph/retrieval modules, bypassing A_Memorix config init."""
    source_root = Path(__file__).resolve().parents[1] / "src"
    memorix_root = source_root / "A_memorix"
    core_root = memorix_root / "core"
    utils_root = core_root / "utils"
    storage_root = core_root / "storage"
    retrieval_root = core_root / "retrieval"
    alias = "_isolated_graph_cache"

    stubbed_src_modules: list[str] = []
    for name, path in (
        ("src", source_root),
        ("src.common", source_root / "common"),
    ):
        if name not in sys.modules:
            _package(name, path)
            stubbed_src_modules.append(name)
    if "src.common.logger" not in sys.modules:
        logger_module = types.ModuleType("src.common.logger")

        class _Logger:
            def __getattr__(self, _name: str):
                return lambda *_args, **_kwargs: None

        logger_module.get_logger = lambda _name: _Logger()  # type: ignore[attr-defined]
        sys.modules["src.common.logger"] = logger_module
        stubbed_src_modules.append("src.common.logger")

    _package(alias)
    _package(f"{alias}.core", core_root)
    _package(f"{alias}.core.utils", utils_root)
    storage_package = _package(f"{alias}.core.storage", storage_root)
    _package(f"{alias}.core.retrieval", retrieval_root)

    try:
        matcher_module = _load_module(
            f"{alias}.core.utils.matcher", utils_root / "matcher.py"
        )
        graph_module = _load_module(
            f"{alias}.core.storage.graph_store", storage_root / "graph_store.py"
        )
        storage_package.GraphStore = graph_module.GraphStore  # type: ignore[attr-defined]
        storage_package.SparseMatrixFormat = graph_module.SparseMatrixFormat  # type: ignore[attr-defined]
        storage_package.VectorStore = object  # type: ignore[attr-defined]
        storage_package.MetadataStore = object  # type: ignore[attr-defined]

        pagerank_module = _load_module(
            f"{alias}.core.retrieval.pagerank", retrieval_root / "pagerank.py"
        )

        def _stub_module(name: str, **values: Any) -> types.ModuleType:
            module = types.ModuleType(name)
            for key, value in values.items():
                setattr(module, key, value)
            sys.modules[name] = module
            return module

        class _StubConfig:
            def __init__(self, **kwargs: Any):
                self.__dict__.update(kwargs)

        _stub_module(
            f"{alias}.core.embedding",
            EmbeddingAPIAdapter=object,
        )
        _stub_module(
            f"{alias}.core.utils.metadata",
            coerce_metadata_dict=lambda value: value if isinstance(value, dict) else {},
        )
        _stub_module(
            f"{alias}.core.utils.time_parser",
            format_timestamp=lambda value: str(value),
        )
        _stub_module(
            f"{alias}.core.retrieval.graph_relation_recall",
            GraphRelationRecallConfig=_StubConfig,
            GraphRelationRecallService=object,
        )
        _stub_module(
            f"{alias}.core.retrieval.posterior_graph",
            PosteriorGraphConfig=_StubConfig,
            apply_posterior_graph_gate=lambda *args, **kwargs: args[0] if args else [],
        )
        _stub_module(
            f"{alias}.core.retrieval.sparse_bm25",
            SparseBM25Config=_StubConfig,
            SparseBM25Index=object,
        )
        dual_path_module = _load_module(
            f"{alias}.core.retrieval.dual_path", retrieval_root / "dual_path.py"
        )
        return (
            graph_module.GraphStore,
            pagerank_module.PersonalizedPageRank,
            dual_path_module.DualPathRetriever,
            matcher_module.AhoCorasick,
        )
    finally:
        for name in reversed(stubbed_src_modules):
            sys.modules.pop(name, None)


GraphStore, PersonalizedPageRank, DualPathRetriever, _AhoCorasick = _load_isolated_modules()


def _make_dual_retriever(graph_store: Any) -> Any:
    retriever = DualPathRetriever.__new__(DualPathRetriever)
    retriever.graph_store = graph_store
    retriever.config = SimpleNamespace(
        ppr_alpha=0.85,
        ppr_local_enabled=False,
        ppr_local_max_nodes=64,
        ppr_local_hops=2,
        ppr_local_min_graph_nodes=128,
    )
    retriever._ppr_semaphore = asyncio.Semaphore(1)
    retriever._ppr_cache = {}
    retriever._ppr_cache_ttl_seconds = 300.0
    retriever._ppr_cache_max_entries = 256
    retriever._ac_matcher = None
    retriever._ac_nodes_count = 0
    retriever._ac_node_revision = None
    retriever._ac_node_map = {}
    return retriever


class GraphRevisionTests(unittest.TestCase):
    def test_mutators_advance_only_relevant_revisions_and_batch_coalesces(self):
        graph = GraphStore()
        self.assertEqual((graph.graph_revision, graph.node_revision), (0, 0))

        graph.add_nodes(["Alpha", "Beta"])
        after_nodes = (graph.graph_revision, graph.node_revision)
        self.assertEqual(after_nodes, (1, 1))
        graph.add_nodes(["alpha"])
        self.assertEqual((graph.graph_revision, graph.node_revision), after_nodes)

        graph.add_edges([("Alpha", "Beta")], [1.0], relation_hashes=["rel-1"])
        after_edge = (graph.graph_revision, graph.node_revision)
        self.assertEqual(after_edge, (2, 1))
        with graph.batch_update():
            graph.add_edges([("Alpha", "Beta")], [1.0], relation_hashes=["rel-2"])
        # Relation-hash metadata alone does not affect numeric PageRank input.
        self.assertEqual((graph.graph_revision, graph.node_revision), after_edge)

        with graph.batch_update():
            self.assertFalse(graph.graph_revision_stable)
            graph.add_nodes(["Gamma"])
            graph.add_edges([("Beta", "Gamma")], [2.0])
            graph.update_edge_weight("Beta", "Gamma", 1.0)
        self.assertTrue(graph.graph_revision_stable)
        self.assertEqual((graph.graph_revision, graph.node_revision), (3, 2))

        unchanged = (graph.graph_revision, graph.node_revision)
        with graph.batch_update():
            graph.add_nodes(["gamma"])
            graph.add_edges([("Beta", "Gamma")], [3.0])
        self.assertEqual((graph.graph_revision, graph.node_revision), unchanged)

    def test_weight_edits_refresh_transpose_and_saliency_caches(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B", "C"])
        graph.add_edges([("A", "B"), ("A", "C")], [1.0, 1.0])
        self.assertEqual(graph.get_in_neighbors("B"), ["A"])
        graph.get_saliency_scores()
        self.assertFalse(graph._adjacency_dirty)
        self.assertIsNotNone(graph._saliency_cache)

        revision = graph.graph_revision
        graph.update_edge_weight("A", "B", 1.0)
        self.assertEqual(graph.graph_revision, revision + 1)
        self.assertTrue(graph._adjacency_dirty)
        self.assertIsNone(graph._saliency_cache)

        graph.get_in_neighbors("B")
        self.assertFalse(graph._adjacency_dirty)
        self.assertEqual(
            graph._adjacency_T[graph._node_to_idx["b"], graph._node_to_idx["a"]],
            graph.get_edge_weight("A", "B"),
        )
        revision = graph.graph_revision
        graph.update_edge_weight("A", "B", 0.0)
        self.assertEqual(graph.graph_revision, revision)

    def test_deactivate_decay_and_node_delete_revisions(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B", "C"])
        graph.add_edges([("A", "B")], [2.0])

        revision = graph.graph_revision
        self.assertEqual(graph.deactivate_edges([("A", "B")]), 1)
        self.assertEqual(graph.graph_revision, revision + 1)
        graph.deactivate_edges([("A", "B")])
        self.assertEqual(graph.graph_revision, revision + 1)

        graph.add_edges([("A", "B")], [2.0])
        revision = graph.graph_revision
        graph.decay(0.5)
        self.assertEqual(graph.graph_revision, revision + 1)
        graph.decay(1.0)
        self.assertEqual(graph.graph_revision, revision + 1)

        node_revision = graph.node_revision
        revision = graph.graph_revision
        graph.delete_nodes(["C"])
        self.assertEqual(graph.node_revision, node_revision + 1)
        self.assertEqual(graph.graph_revision, revision + 1)

    def test_warmed_derived_caches_invalidate_inside_batch(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B", "C"])
        graph.add_edges([("A", "B"), ("A", "C")], [1.0, 1.0])
        graph.get_in_neighbors("B")
        graph.get_saliency_scores()
        revision = graph.graph_revision

        with graph.batch_update():
            graph.update_edge_weight("A", "B", 1.0)
            self.assertTrue(graph._adjacency_dirty)
            self.assertIsNone(graph._saliency_cache)
            graph.get_in_neighbors("B")
            self.assertFalse(graph._adjacency_dirty)
            graph.get_saliency_scores()
            self.assertIsNotNone(graph._saliency_cache)

        self.assertEqual(graph.graph_revision, revision + 1)
        self.assertTrue(graph.graph_revision_stable)

    def test_deletes_clear_and_load_update_revisions_without_resetting_them(self):
        graph = GraphStore()
        graph.add_nodes(["Old", "Peer"])
        graph.add_edges([("Old", "Peer")], [1.0])
        self.assertEqual(graph.delete_edges([("Old", "Peer")]), 1)
        edge_revision = graph.graph_revision
        self.assertEqual(graph.delete_edges([("Old", "Peer")]), 0)
        self.assertEqual(graph.graph_revision, edge_revision)

        with self.subTest("load and clear"):
            import tempfile

            with tempfile.TemporaryDirectory() as temp_dir:
                source = GraphStore()
                source.add_nodes(["Loaded", "Neighbor"])
                source.add_edges([("Loaded", "Neighbor")], [2.0])
                source.save(temp_dir)

                graph.add_nodes(["Extra"])
                before_load = (graph.graph_revision, graph.node_revision)
                graph.load(temp_dir)
                after_load = (graph.graph_revision, graph.node_revision)
                self.assertGreater(after_load[0], before_load[0])
                self.assertEqual(after_load[1], before_load[1] + 1)

                graph.load(temp_dir)
                self.assertEqual((graph.graph_revision, graph.node_revision), after_load)

                graph.clear()
                after_clear = (graph.graph_revision, graph.node_revision)
                self.assertEqual(after_clear, (after_load[0] + 1, after_load[1] + 1))
                graph.clear()
                self.assertEqual((graph.graph_revision, graph.node_revision), after_clear)

    def test_load_dimension_repair_advances_only_graph_revision(self):
        import tempfile

        from scipy.sparse import csr_matrix, save_npz

        with tempfile.TemporaryDirectory() as temp_dir:
            source = GraphStore()
            source.add_nodes(["A", "B"])
            source.add_edges([("A", "B")], [1.0])
            source.save(temp_dir)
            save_npz(
                Path(temp_dir) / "graph_adjacency.npz",
                csr_matrix(([1.0, 3.0], ([0, 2], [1, 2])), shape=(3, 3)),
            )

            target = GraphStore()
            target.add_nodes(["A", "B"])
            target.add_edges([("A", "B")], [4.0])
            graph_revision = target.graph_revision
            node_revision = target.node_revision
            target.load(temp_dir)

        self.assertEqual(target.node_revision, node_revision)
        self.assertEqual(target.graph_revision, graph_revision + 1)
        self.assertEqual(target.num_nodes, 2)
        self.assertEqual(target.get_edge_weight("A", "B"), 0.0)

    def test_same_count_node_replacement_rebuilds_both_matchers(self):
        graph = GraphStore()
        graph.add_nodes(["OldName"])
        ppr = PersonalizedPageRank(graph)
        dual = _make_dual_retriever(graph)

        self.assertEqual(ppr._extract_entities_from_query("OldName"), ["OldName"])
        self.assertEqual(dual._extract_entities("OldName"), {"OldName": 1.0})
        old_ppr_matcher = ppr._ac_matcher
        old_dual_matcher = dual._ac_matcher

        graph.delete_nodes(["OldName"])
        graph.add_nodes(["NewName"])
        self.assertEqual(graph.num_nodes, 1)
        self.assertEqual(ppr._extract_entities_from_query("NewName"), ["NewName"])
        self.assertEqual(dual._extract_entities("NewName"), {"NewName": 1.0})
        self.assertIsNot(ppr._ac_matcher, old_ppr_matcher)
        self.assertIsNot(dual._ac_matcher, old_dual_matcher)
        self.assertEqual(ppr._extract_entities_from_query("OldName"), [])
        self.assertEqual(dual._extract_entities("OldName"), {})

    def test_node_replacement_inside_batch_does_not_reuse_matcher(self):
        graph = GraphStore()
        graph.add_nodes(["Before"])
        ppr = PersonalizedPageRank(graph)
        dual = _make_dual_retriever(graph)
        ppr._extract_entities_from_query("Before")
        dual._extract_entities("Before")

        with graph.batch_update():
            graph.delete_nodes(["Before"])
            graph.add_nodes(["During"])
            self.assertEqual(ppr._extract_entities_from_query("During"), ["During"])
            self.assertEqual(dual._extract_entities("During"), {"During": 1.0})
            graph.delete_nodes(["During"])
            graph.add_nodes(["After"])
            self.assertEqual(ppr._extract_entities_from_query("After"), ["After"])
            self.assertEqual(dual._extract_entities("After"), {"After": 1.0})

        self.assertEqual(ppr._extract_entities_from_query("After"), ["After"])
        self.assertEqual(dual._extract_entities("After"), {"After": 1.0})

    def test_ppr_cache_hits_unchanged_graph_and_misses_same_count_weight_edit(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B", "C"])
        graph.add_edges([("A", "B"), ("A", "C")], [1.0, 1.0])
        retriever = _make_dual_retriever(graph)
        compute_count = 0

        def compute(entities):
            nonlocal compute_count
            compute_count += 1
            return graph.compute_pagerank(personalization=entities)

        retriever._compute_ppr_scores = compute
        first = asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0))
        hit = asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0))
        self.assertEqual(compute_count, 1)
        self.assertEqual(first, hit)

        edge_count = graph.num_edges
        old_revision = graph.graph_revision
        graph.update_edge_weight("A", "B", 1.0)
        self.assertEqual(graph.num_edges, edge_count)
        self.assertGreater(graph.graph_revision, old_revision)
        updated = asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0))
        self.assertEqual(compute_count, 2)
        self.assertNotEqual(first, updated)

    def test_ppr_cache_is_bypassed_while_a_batch_is_active(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B"])
        graph.add_edges([("A", "B")], [1.0])
        retriever = _make_dual_retriever(graph)
        compute_count = 0

        def compute(entities):
            nonlocal compute_count
            compute_count += 1
            return graph.compute_pagerank(personalization=entities)

        retriever._compute_ppr_scores = compute
        asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0))
        with graph.batch_update():
            graph.update_edge_weight("A", "B", 1.0)
            self.assertEqual(
                asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0)),
                {},
            )
        self.assertEqual(compute_count, 1)
        asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0))
        self.assertEqual(compute_count, 2)

    def test_cache_hit_is_skipped_during_mutator_before_revision_commit(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B"])
        graph.add_edges([("A", "B")], [1.0])
        retriever = _make_dual_retriever(graph)
        retriever._compute_ppr_scores = lambda entities: graph.compute_pagerank(
            personalization=entities
        )
        asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0))

        mutation_paused = threading.Event()
        resume_mutation = threading.Event()
        original_mark_changed = graph._mark_graph_changed

        def pause_before_revision_commit():
            mutation_paused.set()
            if not resume_mutation.wait(timeout=5.0):
                raise TimeoutError("test did not release the graph mutator")
            original_mark_changed()

        graph._mark_graph_changed = pause_before_revision_commit
        mutation = threading.Thread(
            target=graph.update_edge_weight,
            args=("A", "B", 1.0),
            daemon=True,
        )
        mutation.start()
        try:
            self.assertTrue(mutation_paused.wait(timeout=2.0))
            self.assertFalse(graph.graph_revision_stable)
            self.assertEqual(
                asyncio.run(retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=2.0)),
                {},
            )
        finally:
            resume_mutation.set()
            mutation.join(timeout=2.0)
            graph._mark_graph_changed = original_mark_changed
        self.assertFalse(mutation.is_alive())

        self.assertEqual(graph.get_edge_weight("A", "B"), 2.0)

    def test_concurrent_mutation_during_ppr_compute_retries_and_caches_current_revision(self):
        graph = GraphStore()
        graph.add_nodes(["A", "B", "C"])
        graph.add_edges([("A", "B"), ("A", "C")], [1.0, 1.0])
        retriever = _make_dual_retriever(graph)
        compute_started = threading.Event()
        resume_compute = threading.Event()
        compute_count = 0

        def compute(entities):
            nonlocal compute_count
            compute_count += 1
            if compute_count == 1:
                compute_started.set()
                if not resume_compute.wait(timeout=5.0):
                    raise TimeoutError("test did not release the in-flight computation")
            return graph.compute_pagerank(personalization=entities)

        retriever._compute_ppr_scores = compute

        async def retrieve_after_mutation():
            task = asyncio.create_task(
                retriever._get_cached_ppr_scores({"A": 1.0}, timeout_s=3.0)
            )
            started = await asyncio.to_thread(compute_started.wait, 2.0)
            self.assertTrue(started, "PPR computation did not start")
            graph.update_edge_weight("A", "B", 1.0)
            resume_compute.set()
            return await task

        scores = asyncio.run(retrieve_after_mutation())
        self.assertEqual(compute_count, 2)
        self.assertEqual(scores, graph.compute_pagerank(personalization={"A": 1.0}))
        self.assertEqual(len(retriever._ppr_cache), 1)
        self.assertEqual(next(iter(retriever._ppr_cache)), retriever._build_ppr_cache_key({"A": 1.0}))


if __name__ == "__main__":
    unittest.main()
