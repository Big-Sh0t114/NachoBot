"""
NachoBot WebUI — Knowledge Base Manager
Manages raw knowledge text files and displays embedding/RAG statistics.
"""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .secure_paths import ensure_within, resolve_named_file
except ImportError:
    from secure_paths import ensure_within, resolve_named_file

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "NachoBot" / "data"
KNOWLEDGE_DIR = DATA_DIR / "lpmm_raw_data"
EMBEDDING_DIR = DATA_DIR / "embedding"
RAG_DIR = DATA_DIR / "rag"
BACKUP_DIR = DATA_DIR / "lpmm_raw_data" / ".backups"


class KnowledgeManager:
    """Manages knowledge base raw files and statistics."""

    def list_files(self) -> list[dict[str, Any]]:
        """List all knowledge base text files."""
        if not KNOWLEDGE_DIR.exists():
            return []

        files = []
        for f in sorted(KNOWLEDGE_DIR.iterdir()):
            if f.is_file() and f.suffix == ".txt":
                stat = f.stat()
                files.append({
                    "filename": f.name,
                    "size_bytes": stat.st_size,
                    "size_display": self._format_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        return files

    def read_file(self, filename: str) -> str:
        """Read a knowledge file's content."""
        path = self._resolve_path(filename)
        # codeql[py/path-injection]
        return path.read_text(encoding="utf-8")

    def update_file(self, filename: str, content: str) -> None:
        """Update a knowledge file (with automatic backup)."""
        path = self._resolve_path(filename)
        self._backup(path)
        # codeql[py/path-injection]
        path.write_text(content, encoding="utf-8")

    def create_file(self, filename: str, content: str = "") -> None:
        """Create a new knowledge file."""
        path = self._resolve_new_path(filename)
        # codeql[py/path-injection]
        if path.exists():
            raise ValueError(f"File already exists: {path.name}")
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        # codeql[py/path-injection]
        path.write_text(content, encoding="utf-8")

    def get_stats(self) -> dict[str, Any]:
        """Return knowledge base and embedding statistics."""
        stats: dict[str, Any] = {
            "knowledge_files": 0,
            "total_knowledge_size": "0 B",
            "embedding": {},
            "rag": {},
        }

        # Knowledge files
        if KNOWLEDGE_DIR.exists():
            txt_files = [f for f in KNOWLEDGE_DIR.iterdir() if f.is_file() and f.suffix == ".txt"]
            stats["knowledge_files"] = len(txt_files)
            total_size = sum(f.stat().st_size for f in txt_files)
            stats["total_knowledge_size"] = self._format_size(total_size)

        # Embedding stats (count items from parquet metadata)
        for ns in ("paragraph", "entity", "relation"):
            parquet_path = EMBEDDING_DIR / f"{ns}.parquet"
            index_path = EMBEDDING_DIR / f"{ns}.index"
            entry: dict[str, Any] = {"items": 0, "index_exists": False}
            if parquet_path.exists():
                try:
                    entry["items"] = self._count_parquet_rows(parquet_path)
                except Exception:
                    entry["items"] = -1  # Error reading
                entry["size"] = self._format_size(parquet_path.stat().st_size)
            if index_path.exists():
                entry["index_exists"] = True
                entry["index_size"] = self._format_size(index_path.stat().st_size)
            stats["embedding"][ns] = entry

        # RAG graph stats
        graphml_path = RAG_DIR / "rag-graph.graphml"
        if graphml_path.exists():
            try:
                nodes, edges = self._count_graphml(graphml_path)
                stats["rag"]["nodes"] = nodes
                stats["rag"]["edges"] = edges
            except Exception:
                stats["rag"]["nodes"] = -1
                stats["rag"]["edges"] = -1
            stats["rag"]["size"] = self._format_size(graphml_path.stat().st_size)

        return stats

    # ---- internal helpers ----

    def _resolve_path(self, filename: str) -> Path:
        """Resolve and validate a knowledge file path."""
        path = resolve_named_file(KNOWLEDGE_DIR, filename, suffix=".txt", must_exist=True)
        # codeql[py/path-injection]
        if not path.is_file():
            raise FileNotFoundError(f"Knowledge file not found: {filename}")
        return path

    def _resolve_new_path(self, filename: str) -> Path:
        """Resolve and validate a new knowledge file path."""
        return resolve_named_file(KNOWLEDGE_DIR, filename, suffix=".txt")

    def _backup(self, path: Path) -> None:
        """Create a timestamped backup of a file."""
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = ensure_within(BACKUP_DIR, BACKUP_DIR / f"{path.stem}_{ts}{path.suffix}")
        # codeql[py/path-injection]
        shutil.copy2(path, backup_path)

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / 1024 / 1024:.1f} MB"

    def _count_parquet_rows(self, path: Path) -> int:
        """Count rows in a parquet file without loading all data."""
        try:
            import pyarrow.parquet as pq
            meta = pq.read_metadata(str(path))
            return meta.num_rows
        except ImportError:
            # Fallback: read with pandas
            import pandas as pd
            df = pd.read_parquet(str(path), engine="pyarrow", columns=[])
            return len(df)

    def _count_graphml(self, path: Path) -> tuple[int, int]:
        """Count nodes and edges in a graphml file by scanning XML tags."""
        nodes = 0
        edges = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("<node "):
                    nodes += 1
                elif stripped.startswith("<edge "):
                    edges += 1
        return nodes, edges
