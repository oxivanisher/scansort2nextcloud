"""Background jobs: nightly archive sync + retrain, periodic reconciliation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scansort.archive import refresh_folders, sync_and_train
from scansort.filing import reconcile_all
from scansort.services import Services

log = logging.getLogger(__name__)


JOBS = {"sync": sync_and_train, "refresh_folders": refresh_folders, "reconcile": reconcile_all}


def _safe(name):
    """Run job ``name`` through the tracker (at most one run at a time), logging failures."""
    def run(svc: Services):
        try:
            svc.jobs.run(name, JOBS[name], svc)
        except Exception:  # noqa: BLE001
            log.exception("job failed", extra={"job": name})

    return run


class Scheduler:
    def __init__(self, svc: Services):
        self.svc = svc
        self._sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1,
                                                        "misfire_grace_time": 3600})

    def start(self) -> None:
        s = self.svc.settings
        self._sched.add_job(
            _safe("sync"), CronTrigger.from_crontab(s.classifier.retrain_cron),
            args=[self.svc], id="nightly_sync",
        )
        self._sched.add_job(
            _safe("reconcile"), "interval", seconds=s.nextcloud.reconcile_interval,
            args=[self.svc], id="reconcile",
            next_run_time=datetime.now() + timedelta(seconds=30),
        )
        if s.classifier.sync_on_startup and not self.svc.db.get_kv("last_archive_sync"):
            log.info("no archive sync yet; scheduling initial sync")
            self.trigger_sync(delay=5)
        self._sched.start()

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def trigger_sync(self, delay: float = 0) -> None:
        self.svc.jobs.queue("sync")
        self._sched.add_job(
            _safe("sync"), "date", args=[self.svc], id="manual_sync",
            replace_existing=True, run_date=datetime.now() + timedelta(seconds=delay),
        )

    def trigger_refresh_folders(self) -> None:
        self.svc.jobs.queue("refresh_folders")
        self._sched.add_job(
            _safe("refresh_folders"), "date", args=[self.svc], id="refresh_folders",
            replace_existing=True, run_date=datetime.now(),
        )

    def trigger_reconcile(self) -> None:
        self.svc.jobs.queue("reconcile")
        self._sched.add_job(
            _safe("reconcile"), "date", args=[self.svc], id="manual_reconcile",
            replace_existing=True, run_date=datetime.now(),
        )

    def next_run(self, job_id: str) -> datetime | None:
        job = self._sched.get_job(job_id)
        return job.next_run_time if job else None
