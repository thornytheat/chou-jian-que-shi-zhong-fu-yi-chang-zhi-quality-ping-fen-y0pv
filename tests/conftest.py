"""测试公共夹具：把数据目录指向临时路径，避免污染运行库。"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QUALISCAN_DATA_DIR", tempfile.mkdtemp(prefix="qualiscan-test-"))

import pytest  # noqa: E402

from app import loader  # noqa: E402


@pytest.fixture(scope="session")
def rules() -> dict:
    return loader.load_rules()


@pytest.fixture(scope="session")
def dataset(rules) -> dict:
    return loader.load_sample(rules)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def poll(client, run_id: str, timeout: float = 30.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/runs/{run_id}/status").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 超时未完成")
