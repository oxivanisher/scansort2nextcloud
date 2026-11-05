"""Service container shared by the worker threads, scheduler and web UI."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import date

from sqlalchemy import func, select

from scansort.classifier import ModelStore, Prediction, propose
from scansort.companies import CompanyIndex, apply_companies
from scansort.config import Settings
from scansort.db import Database, Folder
from scansort.labels import LabelRules, title_from_label
from scansort.naming import build_filename
from scansort.webdav import NextcloudClient

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings: Settings, nc: NextcloudClient | None = None, db: Database | None = None):
        self.settings = settings
        s = settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database(s.db_path)
        self.nc = nc or NextcloudClient(
            s.nextcloud.url,
            s.nextcloud.user,
            s.nextcloud.password.get_secret_value(),
            verify_tls=s.nextcloud.verify_tls,
            timeout=s.nextcloud.timeout,
            retries=s.nextcloud.retries,
        )
        self.labels = LabelRules(
            root=s.nextcloud.root,
            year_folder_pattern=s.classifier.year_folder_pattern,
            exclude=s.classifier.exclude,
            excluded_paths=[s.nextcloud.review_folder, s.nextcloud.trash_folder or ""],
            include=s.classifier.include,
        )
        self.model = ModelStore(s.model_path)
        self.sync_lock = threading.Lock()
        self.worker = None  # set by app startup (IngestWorker)
        self.scheduler = None  # set by app startup (Scheduler)
        self._review_ready = False
        self._nc_ok: tuple[float, bool] = (0.0, False)

    # lifecycle ---------------------------------------------------------------------------------
    def init_storage(self) -> None:
        self.db.migrate()
        tmp = self.settings.tmp_dir
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        self.settings.watch.dir.mkdir(parents=True, exist_ok=True)
        self.model.load()

    def ensure_review_folder(self) -> None:
        if not self._review_ready:
            self.nc.makedirs(self.settings.nextcloud.review_folder)
            self._review_ready = True

    def nextcloud_reachable(self, max_age: float = 60) -> bool:
        ts, ok = self._nc_ok
        if time.monotonic() - ts > max_age:
            ok = self.nc.ping()
            self._nc_ok = (time.monotonic(), ok)
        return ok

    # domain helpers ----------------------------------------------------------------------------
    def company_index(self) -> CompanyIndex:
        with self.db.session() as sess:
            rows = sess.execute(select(Folder.label, func.sum(Folder.doc_count)).group_by(Folder.label)).all()
        labels = {lbl: int(n or 0) for lbl, n in rows if not self.labels.is_label_excluded(lbl)}
        return CompanyIndex(labels, self.settings.classifier.company_folders)

    def propose(self, text: str, sender: str | None) -> list[Prediction]:
        c = self.settings.classifier
        preds = propose(
            text, sender, store=self.model, rules=c.rules, top_n=c.top_n + 2,
            is_excluded=self.labels.is_label_untrained,
        )
        if not (c.company_folders or c.new_company_folder):
            return preds[: c.top_n]
        return apply_companies(
            preds, self.company_index(), text=text, sender=sender, top_n=c.top_n,
            new_company_folder=c.new_company_folder, is_excluded=self.labels.is_label_excluded,
        )

    def filename_for(self, label: str | None, doc_date: date | None, sender: str | None) -> str:
        n = self.settings.naming
        return build_filename(
            n.template, doc_date=doc_date, sender=sender, title=title_from_label(label),
            max_length=n.max_length, fallback_title=n.default_title,
        )

    def close(self) -> None:
        try:
            self.nc.close()
        finally:
            self.db.engine.dispose()
