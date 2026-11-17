import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
os.environ["MINI_OBD_FIXTURES"] = str(REPO / "fixtures")

from app.adapters.simulator import SimulatorTransport  # noqa: E402
from app.obd.client import ObdClient  # noqa: E402
from app.storage.db import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite3")
    yield s
    s.close()


@pytest.fixture
def sim_client():
    def make(scenario="healthy_idle", **kw):
        t = SimulatorTransport(scenario, default_timeout_s=2.0)
        c = ObdClient(t, **kw)
        c.initialize()
        c.discover()
        return c

    return make
