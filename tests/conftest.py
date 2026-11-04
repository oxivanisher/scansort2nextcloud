from __future__ import annotations

import pytest

from scansort.config import load_settings
from scansort.services import Services
from scansort.web.auth import hash_password
from tests.helpers import FakeNextcloud

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("SCANSORT_"):
            monkeypatch.delenv(key)


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def settings(tmp_path, password_hash):
    return load_settings(
        tmp_path / "missing.yaml",
        data_dir=tmp_path / "data",
        watch={"dir": tmp_path / "inbox", "stable_seconds": 0, "poll_interval": 0.2, "use_inotify": False},
        nextcloud={"url": "https://cloud.example.test", "user": "scanbot", "password": "pw",
                   "root": "/Dokumente", "review_folder": "/Dokumente/_Review"},
        ocr={"enabled": False},
        extraction={"known_senders": [{"name": "Swisscom", "patterns": ["swisscom"]}]},
        classifier={"sync_on_startup": False,
                    "rules": [{"pattern": "(?i)steuererkl(ä|ae)rung", "label": "Steuern/{year}"}]},
        webui={"password_hash": password_hash, "session_secret": "test-secret"},
    )


@pytest.fixture
def nc() -> FakeNextcloud:
    fake = FakeNextcloud()
    fake.add_dir("/Dokumente")
    return fake


@pytest.fixture
def svc(settings, nc):
    s = Services(settings, nc=nc)
    s.init_storage()
    yield s
    s.close()
