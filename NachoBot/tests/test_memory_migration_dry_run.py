from __future__ import annotations

import hashlib
import importlib.util
import os
import pickle
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "src" / "A_memorix" / "scripts"
PATHS_FILE = PROJECT_ROOT / "src" / "A_memorix" / "paths.py"
BOOTSTRAP_FILE = SCRIPTS_DIR / "_bootstrap.py"


def _load_module_from_file(module_name: str, source_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_bootstrap_reuses_pure_plugin_path_helpers():
    paths = _load_module_from_file("test_a_memorix_paths", PATHS_FILE)
    bootstrap = _load_module_from_file("test_a_memorix_bootstrap", BOOTSTRAP_FILE)

    for helper in (bootstrap.config_path, bootstrap.default_data_dir, bootstrap.resolve_repo_path):
        assert Path(helper.__code__.co_filename).resolve() == PATHS_FILE.resolve()
    assert bootstrap.DEFAULT_CONFIG_PATH == paths.config_path()
    assert bootstrap.DEFAULT_DATA_DIR == paths.default_data_dir()

    fallback = PROJECT_ROOT / "custom-fallback"
    absolute_path = PROJECT_ROOT / "already-absolute"
    for raw_path in (None, "", "./relative-data", absolute_path):
        assert bootstrap.resolve_repo_path(raw_path) == paths.resolve_repo_path(raw_path)
    assert bootstrap.resolve_repo_path(None, fallback=fallback) == paths.resolve_repo_path(
        None, fallback=fallback
    )


def _install_guards(tmp_path: Path, blocked_imports: tuple[str, ...]) -> tuple[dict[str, str], Path]:
    guard_dir = tmp_path / "import_guard"
    guard_dir.mkdir()
    marker = tmp_path / "blocked_import.txt"
    (guard_dir / "sitecustomize.py").write_text(
        "import importlib.abc\n"
        "import os\n"
        "import sqlite3\n"
        "import sys\n"
        "blocked = set(filter(None, os.environ.get('NACHOBOT_TEST_BLOCKED_IMPORTS', '').split(';')))\n"
        "marker = os.environ['NACHOBOT_TEST_BLOCKED_MARKER']\n"
        "class Guard(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname in blocked:\n"
        "            with open(marker, 'w', encoding='utf-8') as out:\n"
        "                out.write(fullname)\n"
        "            raise RuntimeError('blocked mutating/heavy import: ' + fullname)\n"
        "sys.meta_path.insert(0, Guard())\n"
        "_connect = sqlite3.connect\n"
        "def _read_only_connect(database, *args, **kwargs):\n"
        "    if os.environ.get('NACHOBOT_TEST_REQUIRE_READ_ONLY') == '1':\n"
        "        value = str(database)\n"
        "        if not value.startswith('file:') or 'mode=ro' not in value or not kwargs.get('uri'):\n"
        "            raise RuntimeError('SQLite preview attempted a writable/non-URI connection: ' + value)\n"
        "    return _connect(database, *args, **kwargs)\n"
        "sqlite3.connect = _read_only_connect\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    prior_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(guard_dir) + (os.pathsep + prior_pythonpath if prior_pythonpath else "")
    imports_to_block = tuple(dict.fromkeys((*blocked_imports, "src.common.logger")))
    env["NACHOBOT_TEST_BLOCKED_IMPORTS"] = ";".join(imports_to_block)
    env["NACHOBOT_TEST_BLOCKED_MARKER"] = str(marker)
    env["NACHOBOT_TEST_REQUIRE_READ_ONLY"] = "1"
    return env, marker


def _run_cli(
    tmp_path: Path,
    script_name: str,
    args: list[str],
    *,
    blocked_imports: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    env, marker = _install_guards(tmp_path, blocked_imports)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not marker.exists(), marker.read_text(encoding="utf-8") if marker.exists() else ""
    return result


def _run_guarded_harness(
    tmp_path: Path,
    source: str,
    args: list[str],
    *,
    blocked_imports: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    env, marker = _install_guards(tmp_path, blocked_imports)
    harness_path = tmp_path / "runtime_import_smoke.py"
    harness_path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(harness_path), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not marker.exists(), marker.read_text(encoding="utf-8") if marker.exists() else ""
    return result


def _create_db(path: Path, schema_sql: str, insert_sql: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(schema_sql)
        if insert_sql:
            conn.execute(insert_sql)


def _source_fingerprint(path: Path) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as conn:
        schema = tuple(
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'index', 'trigger', 'view') "
                "ORDER BY type, name"
            )
        )
    return digest, schema


@pytest.mark.parametrize(
    ("script_name", "table_sql", "insert_sql", "expected_preview"),
    [
        (
            "migrate_person_memory_points.py",
            "CREATE TABLE person_info (id INTEGER PRIMARY KEY, person_id TEXT, person_name TEXT, "
            "user_nickname TEXT, memory_points TEXT)",
            "INSERT INTO person_info VALUES (1, 'person-a', 'Alice', 'Alice', '[\"fact:likes tea:1.0\"]')",
            "person_info 待迁移人物: 1 记忆点: 1",
        ),
        (
            "migrate_chat_history.py",
            "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, session_id TEXT, start_timestamp TEXT, "
            "end_timestamp TEXT, participants TEXT, theme TEXT, keywords TEXT, summary TEXT)",
            "INSERT INTO chat_history VALUES (1, 'session-1', '2026-10-01', '2026-10-01', '[]', "
            "'theme', '[]', 'summary')",
            "chat_history 待处理: 1",
        ),
    ],
)
def test_legacy_migration_dry_run_is_read_only_and_skips_sdk_import(
    tmp_path: Path,
    script_name: str,
    table_sql: str,
    insert_sql: str,
    expected_preview: str,
):
    source_db = tmp_path / "source.db"
    target_dir = tmp_path / "missing-target"
    _create_db(source_db, table_sql, insert_sql)
    before = _source_fingerprint(source_db)

    result = _run_cli(
        tmp_path,
        script_name,
        ["--db-path", str(source_db), "--data-dir", str(target_dir), "--dry-run"],
        blocked_imports=(
            "A_memorix.core.runtime.sdk_memory_kernel",
            "src.common.database.database_model",
        ),
    )

    assert expected_preview in result.stdout
    assert _source_fingerprint(source_db) == before
    assert not target_dir.exists()


@pytest.mark.parametrize("script_name", ["migrate_person_memory_points.py", "migrate_chat_history.py"])
def test_legacy_migration_help_does_not_import_sdk_kernel(tmp_path: Path, script_name: str):
    _run_cli(
        tmp_path,
        script_name,
        ["--help"],
        blocked_imports=(
            "A_memorix.core.runtime.sdk_memory_kernel",
            "src.common.database.database_model",
        ),
    )


@pytest.mark.parametrize(
    ("script_name", "table_sql", "insert_sql", "expected_call"),
    [
        (
            "migrate_person_memory_points.py",
            "CREATE TABLE person_info (id INTEGER PRIMARY KEY, person_id TEXT, person_name TEXT, "
            "user_nickname TEXT, memory_points TEXT)",
            "INSERT INTO person_info VALUES (1, 'person-a', 'Alice', 'Alice', '[\"fact:likes tea:1.0\"]')",
            "STUB_INGEST_TEXT",
        ),
        (
            "migrate_chat_history.py",
            "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, session_id TEXT, start_timestamp TEXT, "
            "end_timestamp TEXT, participants TEXT, theme TEXT, keywords TEXT, summary TEXT)",
            "INSERT INTO chat_history VALUES (1, 'session-1', '2026-10-01', '2026-10-01', '[]', "
            "'theme', '[]', 'summary')",
            "STUB_INGEST_SUMMARY",
        ),
    ],
)
def test_sdk_migration_write_branch_resolves_stubbed_kernel_without_host_startup(
    tmp_path: Path,
    script_name: str,
    table_sql: str,
    insert_sql: str,
    expected_call: str,
):
    source_db = tmp_path / "source.db"
    target_dir = tmp_path / "stubbed-target"
    _create_db(source_db, table_sql, insert_sql)
    before = _source_fingerprint(source_db)
    harness = "\n".join(
        [
            "import pathlib, runpy, sys, types",
            "for name in ('A_memorix', 'A_memorix.core', 'A_memorix.core.runtime'):",
            "    package = types.ModuleType(name)",
            "    package.__path__ = []",
            "    sys.modules[name] = package",
            "sdk = types.ModuleType('A_memorix.core.runtime.sdk_memory_kernel')",
            "class SDKMemoryKernel:",
            "    def __init__(self, *args, **kwargs): pass",
            "    async def initialize(self): pass",
            "    async def ingest_text(self, **kwargs): print('STUB_INGEST_TEXT'); return {'stored_ids': ['stub']} ",
            "    async def ingest_summary(self, **kwargs): print('STUB_INGEST_SUMMARY'); return {'stored_ids': ['stub']} ",
            "    def memory_stats(self): return {'stubbed': True}",
            "    def close(self): pass",
            "sdk.SDKMemoryKernel = SDKMemoryKernel",
            "sys.modules[sdk.__name__] = sdk",
            "script_path, *script_args = sys.argv[1:]",
            "sys.path.insert(0, str(pathlib.Path(script_path).resolve().parent))",
            "sys.argv = [script_path, *script_args]",
            "runpy.run_path(script_path, run_name='__main__')",
        ]
    )
    result = _run_guarded_harness(
        tmp_path,
        harness,
        [
            str(SCRIPTS_DIR / script_name),
            "--db-path",
            str(source_db),
            "--data-dir",
            str(target_dir),
        ],
        blocked_imports=("src.common.database.database_model",),
    )

    assert expected_call in result.stdout
    assert "迁移完成: migrated=1 skipped=0" in result.stdout
    assert _source_fingerprint(source_db) == before
    assert not target_dir.exists()


def test_relation_backfill_runtime_loader_binds_deferred_components_from_stub_modules(tmp_path: Path):
    harness = "\n".join(
        [
            "import pathlib, runpy, sys, types",
            "for name in ('A_memorix', 'A_memorix.core', 'A_memorix.core.utils'):",
            "    package = types.ModuleType(name)",
            "    package.__path__ = []",
            "    sys.modules[name] = package",
            "storage = types.ModuleType('A_memorix.core.storage')",
            "class VectorStore: pass",
            "class GraphStore: pass",
            "class MetadataStore: pass",
            "class QuantizationType: pass",
            "class SparseMatrixFormat: pass",
            "storage.VectorStore, storage.GraphStore, storage.MetadataStore = VectorStore, GraphStore, MetadataStore",
            "storage.QuantizationType, storage.SparseMatrixFormat = QuantizationType, SparseMatrixFormat",
            "embedding = types.ModuleType('A_memorix.core.embedding')",
            "def create_embedding_api_adapter(**kwargs): return None",
            "embedding.create_embedding_api_adapter = create_embedding_api_adapter",
            "relation = types.ModuleType('A_memorix.core.utils.relation_write_service')",
            "class RelationWriteService: pass",
            "relation.RelationWriteService = RelationWriteService",
            "sys.modules[storage.__name__] = storage",
            "sys.modules[embedding.__name__] = embedding",
            "sys.modules[relation.__name__] = relation",
            "sys.path.insert(0, str(pathlib.Path(sys.argv[1]).resolve().parent))",
            "namespace = runpy.run_path(sys.argv[1])",
            "loader = namespace['_load_runtime_components']",
            "loader()",
            "bindings = loader.__globals__",
            "assert bindings['VectorStore'] is VectorStore",
            "assert bindings['GraphStore'] is GraphStore",
            "assert bindings['MetadataStore'] is MetadataStore",
            "assert bindings['QuantizationType'] is QuantizationType",
            "assert bindings['SparseMatrixFormat'] is SparseMatrixFormat",
            "assert bindings['create_embedding_api_adapter'] is create_embedding_api_adapter",
            "assert bindings['RelationWriteService'] is RelationWriteService",
            "print('RELATION_BINDINGS_OK')",
        ]
    )
    result = _run_guarded_harness(
        tmp_path,
        harness,
        [str(SCRIPTS_DIR / "backfill_relation_vectors.py")],
        blocked_imports=(
            "A_memorix.core.storage",
            "A_memorix.core.embedding",
            "A_memorix.core.utils.relation_write_service",
        ),
    )

    assert "RELATION_BINDINGS_OK" in result.stdout


def test_release_migration_write_branch_resolves_stub_storage_without_host_startup(tmp_path: Path):
    config_path = tmp_path / "a_memorix.toml"
    config_path.write_text(
        "[routing]\ntool_search_mode = 'forward'\n"
        "[summarization]\nmodel_name = ['auto']\ndefault_knowledge_type = 'narrative'\n"
        "[embedding]\nquantization_type = 'int8'\n"
        "[graph]\nsparse_matrix_format = 'csr'\n",
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    target_dir = tmp_path / "release-target"
    harness = "\n".join(
        [
            "import pathlib, runpy, sys, types",
            "for name in ('A_memorix', 'A_memorix.core'):",
            "    package = types.ModuleType(name)",
            "    package.__path__ = []",
            "    sys.modules[name] = package",
            "storage = types.ModuleType('A_memorix.core.storage')",
            "class GraphStore:",
            "    def __init__(self, **kwargs): print('STUB_GRAPH_STORE')",
            "class MetadataStore: pass",
            "class VectorStore: pass",
            "class QuantizationType: INT8 = object()",
            "storage.GraphStore, storage.MetadataStore = GraphStore, MetadataStore",
            "storage.VectorStore, storage.QuantizationType = VectorStore, QuantizationType",
            "sys.modules[storage.__name__] = storage",
            "sys.path.insert(0, str(pathlib.Path(sys.argv[1]).resolve().parent))",
            "namespace = runpy.run_path(sys.argv[1])",
            "from pathlib import Path",
            "payload = namespace['_migrate_impl'](Path(sys.argv[2]), Path(sys.argv[3]), dry_run=False)",
            "assert payload['dry_run'] is False",
            "print('RELEASE_BINDINGS_OK')",
        ]
    )
    result = _run_guarded_harness(
        tmp_path,
        harness,
        [str(SCRIPTS_DIR / "release_vnext_migrate.py"), str(config_path), str(target_dir)],
        blocked_imports=("A_memorix.core.storage",),
    )

    assert "STUB_GRAPH_STORE" in result.stdout
    assert "RELEASE_BINDINGS_OK" in result.stdout
    assert config_path.read_bytes() == config_before
    assert (target_dir / "vectors").is_dir()
    assert (target_dir / "metadata").is_dir()
    assert (target_dir / "graph").is_dir()


def test_temporal_backfill_dry_run_handles_legacy_schema_without_writes(tmp_path: Path):
    data_dir = tmp_path / "temporal-data"
    db_path = data_dir / "metadata.db"
    _create_db(
        db_path,
        "CREATE TABLE paragraphs (hash TEXT PRIMARY KEY, created_at REAL, source TEXT)",
        "INSERT INTO paragraphs VALUES ('p1', 100.0, 'legacy')",
    )
    before = _source_fingerprint(db_path)

    result = _run_cli(
        tmp_path,
        "backfill_temporal_metadata.py",
        ["--data-dir", str(data_dir), "--dry-run", "--limit", "10"],
        blocked_imports=("A_memorix.core.storage", "A_memorix.core.storage.metadata_store"),
    )

    assert "[dry-run] candidates=1" in result.stdout
    assert _source_fingerprint(db_path) == before
    assert not (data_dir / "vectors").exists()


def test_relation_vector_backfill_dry_run_uses_read_only_legacy_preview(tmp_path: Path):
    data_dir = tmp_path / "relation-data"
    db_path = data_dir / "metadata" / "metadata.db"
    _create_db(
        db_path,
        "CREATE TABLE relations (hash TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, created_at REAL)",
        "INSERT INTO relations VALUES ('relation-1', 'Alice', 'likes', 'tea', 100.0)",
    )
    config_path = tmp_path / "a_memorix.toml"
    config_path.write_text(
        "[embedding]\nquantization_type = 'int8'\ndimension = 16\n"
        "[graph]\nsparse_matrix_format = 'csr'\n"
        "[retrieval.relation_vectorization]\nmax_retry = 3\n",
        encoding="utf-8",
    )
    before = _source_fingerprint(db_path)

    result = _run_cli(
        tmp_path,
        "backfill_relation_vectors.py",
        ["--config", str(config_path), "--data-dir", str(data_dir), "--dry-run"],
        blocked_imports=(
            "A_memorix.core.storage",
            "A_memorix.core.embedding",
            "A_memorix.core.utils.relation_write_service",
        ),
    )

    assert "candidates: 1" in result.stdout
    assert "dry_run=true" in result.stdout
    assert _source_fingerprint(db_path) == before
    assert not (data_dir / "vectors").exists()
    assert not (data_dir / "graph").exists()


def test_relation_vector_dry_run_matches_current_state_and_retry_filters(tmp_path: Path):
    data_dir = tmp_path / "current-relation-data"
    db_path = data_dir / "metadata" / "metadata.db"
    _create_db(
        db_path,
        "CREATE TABLE relations (hash TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, "
        "vector_state TEXT, vector_retry_count INTEGER, vector_updated_at REAL, created_at REAL)",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("failed-1", "s", "p", "o", "failed", 0, 1.0, 1.0),
                ("pending-1", "s", "p", "o", "pending", 2, 2.0, 2.0),
                ("pending-retried", "s", "p", "o", "pending", 3, 3.0, 3.0),
                ("ready-1", "s", "p", "o", "ready", 0, 4.0, 4.0),
            ],
        )
    config_path = tmp_path / "a_memorix.toml"
    config_path.write_text(
        "[embedding]\nquantization_type = 'int8'\ndimension = 16\n"
        "[graph]\nsparse_matrix_format = 'csr'\n"
        "[retrieval.relation_vectorization]\nmax_retry = 3\n",
        encoding="utf-8",
    )
    before = _source_fingerprint(db_path)

    result = _run_cli(
        tmp_path,
        "backfill_relation_vectors.py",
        [
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "--dry-run",
            "--states",
            "failed,pending",
            "--max-retry",
            "3",
        ],
        blocked_imports=(
            "A_memorix.core.storage",
            "A_memorix.core.embedding",
            "A_memorix.core.utils.relation_write_service",
        ),
    )

    assert "candidates: 2 (states=['failed', 'pending'], max_retry=3)" in result.stdout
    assert _source_fingerprint(db_path) == before
    assert not (data_dir / "vectors").exists()


def test_relation_vector_dry_run_counts_only_ready_hashes_missing_from_index(tmp_path: Path):
    data_dir = tmp_path / "ready-relation-data"
    db_path = data_dir / "metadata" / "metadata.db"
    _create_db(
        db_path,
        "CREATE TABLE relations (hash TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, "
        "vector_state TEXT, vector_retry_count INTEGER, vector_updated_at REAL, created_at REAL)",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("ready-present", "s", "p", "o", "ready", 0, 1.0, 1.0),
                ("ready-missing", "s", "p", "o", "ready", 0, 2.0, 2.0),
            ],
        )
    vectors_dir = data_dir / "vectors"
    vectors_dir.mkdir(parents=True)
    with (vectors_dir / "vectors_metadata.pkl").open("wb") as metadata_file:
        pickle.dump(
            {"vector_norm": "l2", "known_hashes": ["ready-present"], "deleted_ids": []},
            metadata_file,
        )
    config_path = tmp_path / "a_memorix.toml"
    config_path.write_text(
        "[embedding]\nquantization_type = 'int8'\ndimension = 16\n"
        "[graph]\nsparse_matrix_format = 'csr'\n"
        "[retrieval.relation_vectorization]\nmax_retry = 3\n",
        encoding="utf-8",
    )
    before = _source_fingerprint(db_path)

    result = _run_cli(
        tmp_path,
        "backfill_relation_vectors.py",
        [
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "--dry-run",
            "--states",
            "none",
            "--include-ready-missing",
        ],
        blocked_imports=(
            "A_memorix.core.storage",
            "A_memorix.core.embedding",
            "A_memorix.core.utils.relation_write_service",
        ),
    )

    assert "candidates: 1 (states=['none'], max_retry=3)" in result.stdout
    assert "ready_missing_candidates_added: 1" in result.stdout
    assert _source_fingerprint(db_path) == before
    assert not (data_dir / "graph").exists()


def test_release_vnext_dry_run_does_not_create_target_storage(tmp_path: Path):
    config_path = tmp_path / "a_memorix.toml"
    config_path.write_text(
        "[storage]\ndata_dir = './legacy'\n"
        "[embedding]\nquantization_type = 'float32'\n"
        "[graph]\nsparse_matrix_format = 'csr'\n"
        "[routing]\ntool_search_mode = 'legacy'\n"
        "[summarization]\nmodel_name = 'auto'\ndefault_knowledge_type = 'narrative'\n",
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    target_dir = tmp_path / "not-created"

    result = _run_cli(
        tmp_path,
        "release_vnext_migrate.py",
        ["--config", str(config_path), "--data-dir", str(target_dir), "migrate", "--dry-run"],
        blocked_imports=("A_memorix.core.storage", "A_memorix.core.storage.metadata_store"),
    )

    assert '"dry_run": true' in result.stdout
    assert config_path.read_bytes() == config_before
    assert not target_dir.exists()


@pytest.mark.parametrize("extra_flags", [[], ["--verify-only"]])
def test_maibot_migration_dry_run_precedes_verify_only_and_writes_nothing(
    tmp_path: Path,
    extra_flags: list[str],
):
    source_db = tmp_path / "maibot.db"
    _create_db(
        source_db,
        "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, session_id TEXT, start_timestamp REAL, "
        "end_timestamp REAL, participants TEXT, theme TEXT, keywords TEXT, summary TEXT)",
        "INSERT INTO chat_history VALUES (1, 'session-1', 100.0, 101.0, '[]', 'theme', '[]', 'summary')",
    )
    before = _source_fingerprint(source_db)
    target_dir = tmp_path / "maibot-target"

    result = _run_cli(
        tmp_path,
        "migrate_maibot_memory.py",
        [
            "--source-db",
            str(source_db),
            "--target-data-dir",
            str(target_dir),
            "--dry-run",
            *extra_flags,
        ],
        blocked_imports=(
            "src.common.database.database_model",
            "src.memory_system.person_profile_service",
            "src.common.logger",
        ),
    )

    assert "=== Migration Preview ===" in result.stdout
    assert _source_fingerprint(source_db) == before
    assert not target_dir.exists()
