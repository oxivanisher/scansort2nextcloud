"""Configuration: YAML file + environment variable overrides (``SCANSORT_`` prefix, ``__`` nesting).

Secrets can also be supplied as files: ``SCANSORT_NEXTCLOUD__PASSWORD_FILE=/run/secrets/nc`` is read
into ``SCANSORT_NEXTCLOUD__PASSWORD`` before the settings are built (Docker secrets).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ENV_PREFIX = "SCANSORT_"
DEFAULT_CONFIG_PATH = "/config/config.yaml"

_config_path: Path | None = None


def _norm_dav_path(value: str) -> str:
    value = "/" + value.strip().strip("/")
    return re.sub(r"/+", "/", value)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WatchSettings(_Section):
    dir: Path = Path("/data/inbox")
    patterns: list[str] = ["*.pdf"]
    ignore_patterns: list[str] = [".*", "*.part", "*.tmp", "~*", "*.filepart"]
    stable_seconds: float = Field(5, ge=0)
    poll_interval: float = Field(10, gt=0)
    use_inotify: bool = True


class IngestSettings(_Section):
    concurrency: int = Field(1, ge=1, le=8)
    retry_backoff: list[int] = [60, 300, 1800, 3600]


class NextcloudSettings(_Section):
    url: str
    user: str
    password: SecretStr = SecretStr("")
    root: str = "/Dokumente"
    review_folder: str = "/Dokumente/_Review"
    trash_folder: str | None = None
    verify_tls: bool = True
    timeout: float = 30
    retries: int = Field(3, ge=0)
    reconcile_interval: int = Field(600, ge=30)

    @field_validator("user")
    @classmethod
    def _user(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nextcloud.user must be set (login name of the machine account)")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.match(r"^https?://", v):
            raise ValueError("nextcloud.url must start with http:// or https://")
        return v

    @field_validator("root", "review_folder")
    @classmethod
    def _paths(cls, v: str) -> str:
        return _norm_dav_path(v)

    @field_validator("trash_folder")
    @classmethod
    def _trash(cls, v: str | None) -> str | None:
        return _norm_dav_path(v) if v else None


class OcrSettings(_Section):
    enabled: bool = True
    languages: list[str] = ["deu", "eng"]
    min_chars: int = 50
    timeout: int = 900
    extra_args: list[str] = []


class KnownSender(_Section):
    name: str
    patterns: list[str] = []


class ExtractionSettings(_Section):
    date_languages: list[str] = ["de", "en"]
    min_year: int = 1990
    date_keywords: list[str] = [
        "Rechnungsdatum", "Datum", "Date", "Invoice date", "Ausstellungsdatum", "Belegdatum", "den",
    ]
    amount_keywords: list[str] = [
        "Total", "Gesamtbetrag", "Betrag", "zu bezahlen", "zahlbar", "Rechnungsbetrag", "Summe",
        "Amount due",
    ]
    known_senders: list[KnownSender] = []


class Rule(_Section):
    pattern: str
    label: str

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"invalid regex {v!r}: {exc}") from exc
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return v.strip().strip("/")


class ClassifierSettings(_Section):
    retrain_cron: str = "0 3 * * *"
    top_n: int = Field(3, ge=1, le=10)
    min_samples: int = Field(2, ge=1)
    year_folder_pattern: str = r"^\d{4}$"
    exclude: list[str] = ["_*"]
    ocr_archive: bool = False
    max_archive_file_mb: float = 50
    sync_on_startup: bool = True
    rules: list[Rule] = []

    @field_validator("retrain_cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(v)
        except ValueError as exc:
            raise ValueError(f"invalid cron expression {v!r}: {exc}") from exc
        return v


class NamingSettings(_Section):
    template: str = "{date:%Y-%m-%d}_{sender}_{title}"
    max_length: int = Field(120, ge=20, le=250)
    default_title: str = "Scan"


class WebUISettings(_Section):
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_hours: int = Field(168, ge=1)
    secure_cookies: Literal["auto", "always", "never"] = "auto"
    forwarded_allow_ips: str = "127.0.0.1"


class LoggingSettings(_Section):
    level: str = "INFO"
    format: Literal["text", "json"] = "text"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
        case_sensitive=False,
    )

    data_dir: Path = Path("/data")
    watch: WatchSettings = WatchSettings()
    ingest: IngestSettings = IngestSettings()
    nextcloud: NextcloudSettings
    ocr: OcrSettings = OcrSettings()
    extraction: ExtractionSettings = ExtractionSettings()
    classifier: ClassifierSettings = ClassifierSettings()
    naming: NamingSettings = NamingSettings()
    webui: WebUISettings = WebUISettings()
    logging: LoggingSettings = LoggingSettings()

    @model_validator(mode="after")
    def _check(self) -> Settings:
        nc = self.nextcloud
        if nc.review_folder == nc.root:
            raise ValueError("nextcloud.review_folder must differ from nextcloud.root")
        try:
            re.compile(self.classifier.year_folder_pattern)
        except re.error as exc:
            raise ValueError(f"classifier.year_folder_pattern is not a valid regex: {exc}") from exc
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if _config_path is not None and _config_path.is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=_config_path))
        return tuple(sources)

    # convenience paths -------------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "scansort.db"

    @property
    def model_path(self) -> Path:
        return self.data_dir / "model.joblib"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


def _load_file_env_vars() -> None:
    """Support ``SCANSORT_FOO__BAR_FILE`` → ``SCANSORT_FOO__BAR`` (Docker secrets)."""
    for key, value in list(os.environ.items()):
        if not key.upper().startswith(ENV_PREFIX) or not key.upper().endswith("_FILE"):
            continue
        target = key[: -len("_FILE")]
        # consume the *_FILE variable, otherwise pydantic-settings treats it as an unknown nested key
        del os.environ[key]
        if target in os.environ:
            continue
        try:
            os.environ[target] = Path(value).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read secret file for {key}: {exc}") from exc


def config_path_from_env() -> Path:
    return Path(os.environ.get("SCANSORT_CONFIG", DEFAULT_CONFIG_PATH))


def load_settings(config_path: Path | str | None = None, **overrides) -> Settings:
    """Build settings from (in increasing priority) the YAML file, env vars and explicit overrides."""
    global _config_path
    _config_path = Path(config_path) if config_path else config_path_from_env()
    _load_file_env_vars()
    return Settings(**overrides)
