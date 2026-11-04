"""Archive sync (training text cache + folder cache) and retraining."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import delete, select

from scansort.classifier import NotEnoughData, Sample, train
from scansort.db import ArchiveFile, Document, Folder, Proposal, Status, utcnow
from scansort.extraction import extract_sender
from scansort.services import Services
from scansort.textextract import meaningful_chars, read_pdf, run_ocr
from scansort.webdav import DavEntry, DavError

log = logging.getLogger(__name__)


@dataclass
class WalkResult:
    folders: dict[str, tuple[str, int]] = field(default_factory=dict)  # path -> (label, pdf count)
    files: list[tuple[DavEntry, str]] = field(default_factory=list)  # (entry, label)


@dataclass
class SyncStats:
    folders: int = 0
    files: int = 0
    downloaded: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    seconds: float = 0.0


def walk(svc: Services) -> WalkResult:
    """Recursive ``PROPFIND Depth: 1`` walk of ``nextcloud.root`` (Depth: infinity is often disabled)."""
    root = svc.settings.nextcloud.root
    result = WalkResult()
    stack = [root]
    while stack:
        folder = stack.pop()
        label = svc.labels.label_for_path(folder)
        count = 0
        for entry in svc.nc.listdir(folder):
            if entry.is_dir:
                if not svc.labels.is_excluded(entry.path):
                    stack.append(entry.path)
            elif entry.name.lower().endswith(".pdf") and label:
                count += 1
                result.files.append((entry, label))
        if label:
            result.folders[folder] = (label, count)
    return result


def _store_folders(svc: Services, folders: dict[str, tuple[str, int]]) -> None:
    now = utcnow()
    with svc.db.session() as sess:
        sess.execute(delete(Folder))
        for path, (label, count) in folders.items():
            sess.add(Folder(path=path, label=label, doc_count=count, synced_at=now))


def refresh_folders(svc: Services) -> int:
    result = walk(svc)
    _store_folders(svc, result.folders)
    svc.db.set_kv("last_folder_refresh", utcnow().isoformat(timespec="seconds"))
    return len(result.folders)


def _archive_text(svc: Services, entry: DavEntry) -> str:
    data = svc.nc.download(entry.path)
    text = read_pdf(data).text
    if svc.settings.classifier.ocr_archive and meaningful_chars(text) < svc.settings.ocr.min_chars:
        with tempfile.TemporaryDirectory(dir=svc.settings.tmp_dir) as tmp:
            src, dst = Path(tmp) / "in.pdf", Path(tmp) / "out.pdf"
            src.write_bytes(data)
            run_ocr(src, dst, svc.settings.ocr.languages, svc.settings.ocr.timeout, svc.settings.ocr.extra_args)
            text = read_pdf(dst).text
    return text


def sync_archive(svc: Services) -> SyncStats:
    started = time.monotonic()
    stats = SyncStats()
    result = walk(svc)
    stats.folders, stats.files = len(result.folders), len(result.files)
    _store_folders(svc, result.folders)

    with svc.db.session() as sess:
        cached = {a.nc_fileid: (a.etag, a.nc_path, a.label) for a in sess.scalars(select(ArchiveFile))}
    max_bytes = svc.settings.classifier.max_archive_file_mb * 1024 * 1024
    seen: set[str] = set()
    for entry, label in result.files:
        if not entry.fileid:
            continue
        seen.add(entry.fileid)
        cur = cached.get(entry.fileid)
        if cur and cur[0] == (entry.etag or ""):
            stats.unchanged += 1
            if cur[1] != entry.path or cur[2] != label:
                with svc.db.session() as sess:
                    row = sess.get(ArchiveFile, entry.fileid)
                    row.nc_path, row.label = entry.path, label
            continue
        text = ""
        if entry.size is None or entry.size <= max_bytes:
            try:
                text = _archive_text(svc, entry)
                stats.downloaded += 1
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                log.warning("archive file skipped", extra={"path": entry.path, "error": str(exc)})
        with svc.db.session() as sess:
            row = sess.get(ArchiveFile, entry.fileid)
            if row is None:
                row = ArchiveFile(nc_fileid=entry.fileid)
                sess.add(row)
            row.nc_path, row.etag, row.label, row.text = entry.path, entry.etag or "", label, text
            row.synced_at = utcnow()

    gone = set(cached) - seen
    if gone:
        with svc.db.session() as sess:
            sess.execute(delete(ArchiveFile).where(ArchiveFile.nc_fileid.in_(gone)))
    stats.removed = len(gone)
    stats.seconds = round(time.monotonic() - started, 1)
    now = utcnow().isoformat(timespec="seconds")
    svc.db.set_kv("last_archive_sync", now)
    svc.db.set_kv("last_folder_refresh", now)
    svc.db.set_kv("last_archive_sync_stats", json.dumps(asdict(stats)))
    log.info("archive sync done", extra=asdict(stats))
    return stats


def training_samples(svc: Services) -> list[Sample]:
    cfg = svc.settings.extraction
    with svc.db.session() as sess:
        rows = sess.execute(select(ArchiveFile.label, ArchiveFile.text).where(ArchiveFile.text != "")).all()
    return [
        Sample(text=text, label=label, sender=extract_sender(text, cfg))
        for label, text in rows
        if not svc.labels.is_label_excluded(label)
    ]


def retrain(svc: Services) -> bool:
    samples = training_samples(svc)
    try:
        pipeline, info = train(samples, svc.settings.classifier.min_samples)
    except NotEnoughData as exc:
        svc.db.set_kv("model_status", str(exc))
        log.warning("not training: %s", exc)
        return False
    svc.model.replace(pipeline, info)
    svc.db.set_kv("model_status", "ok")
    log.info("model trained", extra=asdict(info) | {"skipped_labels": len(info.skipped_labels)})
    repropose_pending(svc)
    return True


def repropose_pending(svc: Services) -> int:
    """Refresh proposals of pending documents with the current model/rules."""
    with svc.db.session() as sess:
        docs = sess.scalars(select(Document).where(Document.status == Status.PENDING)).all()
        for doc in docs:
            preds = svc.propose(doc.text, doc.sender)
            doc.proposals.clear()
            sess.flush()
            doc.proposals.extend(
                Proposal(rank=i, label=p.label, probability=p.probability, source=p.source,
                         model_version=p.model_version)
                for i, p in enumerate(preds)
            )
        return len(docs)


def sync_and_train(svc: Services) -> bool:
    """Archive sync followed by retraining. Returns False if it failed or another sync is running."""
    if not svc.sync_lock.acquire(blocking=False):
        log.info("archive sync already running")
        return False
    try:
        svc.db.set_kv("job_running", "sync")
        sync_archive(svc)
        svc.db.set_kv("last_archive_sync_error", "")
        retrain(svc)
        return True
    except DavError as exc:  # Nextcloud unreachable etc.: expected, no traceback
        svc.db.set_kv("last_archive_sync_error", f"{utcnow().isoformat(timespec='seconds')}: {exc}")
        log.warning("archive sync failed", extra={"error": str(exc)})
        return False
    except Exception as exc:
        svc.db.set_kv("last_archive_sync_error", f"{utcnow().isoformat(timespec='seconds')}: {exc}")
        log.exception("archive sync failed")
        return False
    finally:
        svc.db.set_kv("job_running", "")
        svc.sync_lock.release()


