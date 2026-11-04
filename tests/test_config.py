import pytest
from pydantic import ValidationError

from scansort.config import load_settings

YAML = """
data_dir: /tmp/x
nextcloud:
  url: https://cloud.example.ch/
  user: scanbot
  root: /Dokumente/
  review_folder: Dokumente/_Review
classifier:
  top_n: 5
"""


def test_yaml_and_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(YAML)
    monkeypatch.setenv("SCANSORT_NEXTCLOUD__PASSWORD", "app-pw")
    monkeypatch.setenv("SCANSORT_CLASSIFIER__TOP_N", "2")
    s = load_settings(cfg)
    assert s.nextcloud.url == "https://cloud.example.ch"
    assert s.nextcloud.root == "/Dokumente"
    assert s.nextcloud.review_folder == "/Dokumente/_Review"
    assert s.nextcloud.password.get_secret_value() == "app-pw"
    assert s.classifier.top_n == 2  # env beats yaml
    assert "app-pw" not in repr(s)


def test_file_secret(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(YAML)
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    monkeypatch.setenv("SCANSORT_NEXTCLOUD__PASSWORD_FILE", str(secret))
    assert load_settings(cfg).nextcloud.password.get_secret_value() == "from-file"


def test_env_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANSORT_NEXTCLOUD__URL", "http://nc")
    monkeypatch.setenv("SCANSORT_NEXTCLOUD__USER", "bot")
    s = load_settings(tmp_path / "nope.yaml")
    assert s.nextcloud.user == "bot"


@pytest.mark.parametrize("bad", [
    "nextcloud: {url: ftp://x, user: a}",
    "nextcloud: {url: https://x, user: a}\nclassifier: {retrain_cron: 'not a cron'}",
    "nextcloud: {url: https://x, user: a}\ntypo_key: 1",
    "nextcloud: {url: https://x, user: a, review_folder: /Dokumente}",
])
def test_invalid(tmp_path, bad):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(bad)
    with pytest.raises(ValidationError):
        load_settings(cfg)
