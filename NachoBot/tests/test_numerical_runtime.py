"""Startup policy plus a real Windows native-memory regression check."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from src.common.numerical_runtime import configure_numerical_runtime


def test_preserves_explicit_worker_configuration():
    with patch.dict(os.environ, {"OPENBLAS_NUM_THREADS": "3"}, clear=True):
        configure_numerical_runtime()
        assert os.environ["OPENBLAS_NUM_THREADS"] == "3"


@pytest.mark.skipif(os.name != "nt", reason="Windows native work-buffer regression")
def test_windows_imports_and_query_do_not_reserve_gigabytes():
    code = """
from src.common.numerical_runtime import configure_numerical_runtime
configure_numerical_runtime()
import numpy as np
import faiss
import scipy.linalg
import json
import psutil
index = faiss.IndexFlatIP(32)
vectors = np.eye(32, dtype=np.float32)
index.add(vectors)
faiss.omp_set_num_threads(1)
scores, ids = index.search(vectors[:1], 4)
print(json.dumps(dict(private=psutil.Process().memory_info().private,
                     top_id=int(ids[0, 0]), score=float(scores[0, 0]))))
"""
    environment = os.environ.copy()
    environment.pop("OPENBLAS_NUM_THREADS", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    report = json.loads(result.stdout)
    assert report["private"] < 512 * 1024**2
    assert report["top_id"] == 0
    assert report["score"] == 1.0
