"""
Shared pytest fixtures for Sofia Monitor tests.

We point SOFIA_DB_PATH / SOFIA_CONFIG_PATH at temp files so each test starts
clean and the production data/sofia.db is never touched. We also override the
module-level DB_PATH / CONFIG_PATH on already-loaded modules in case they were
imported before pytest patched os.environ.
"""
import sys
from pathlib import Path

import pytest

# Ensure the backend package is importable regardless of where pytest is run from.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def tmp_sofia_paths(tmp_path, monkeypatch):
    """Override DB and config paths to a per-test temp location."""
    db_path = tmp_path / "sofia.db"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("SOFIA_DB_PATH", str(db_path))
    monkeypatch.setenv("SOFIA_CONFIG_PATH", str(cfg_path))

    # If db_service / config_service were already imported (e.g. transitively
    # by another test), patch their module-level constants directly.
    from app.services import db_service, config_service  # noqa: WPS433
    monkeypatch.setattr(db_service, "DB_PATH", db_path)
    monkeypatch.setattr(config_service, "CONFIG_PATH", cfg_path)

    return {"db_path": db_path, "cfg_path": cfg_path}


@pytest.fixture
async def initialized_db(tmp_sofia_paths):
    from app.services import db_service  # noqa: WPS433
    await db_service.init_db()
    return db_service
