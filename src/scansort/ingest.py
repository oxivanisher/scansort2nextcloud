"""Watch folder → OCR → classify → upload to the Nextcloud review folder."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from scansort.db import Document, IngestAttempt, Proposal, Status, utcnow
from scansort.extraction import extract_fields
from scansort.services import Services
from scansort.textextract import OcrError, meaningful_chars, read_pdf, run_ocr
from scansort.webdav import AlreadyExists

log = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text(svc: Services, path: Path, workdir: Path) -> tuple[str, str, int, Path]:
    """Returns ``(text, text_source, page_count, file_to_upload)``. Never raises for bad PDFs/OCR."""
    s = svc.settings
    text, source, pages, upload = "", "none", 0, path
    try:
        pdf = read_pdf(path)
        text, pages = pdf.text, pdf.page_count
        source = "layer" if text else "none"
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read PDF text layer", extra={"file": path.name, "error": str(exc)})
    if meaningful_chars(text) >= s.ocr.min_chars or not s.ocr.enabled:
        return text, source, pages, upload
    out = workdir / "ocr.pdf"
    try:
        started = time.monotonic()
        run_ocr(path, out, s.ocr.languages, s.ocr.timeout, s.ocr.extra_args)
        ocr_pdf = read_pdf(out)
        log.info("OCR done", extra={"file": path.name, "seconds": round(time.monotonic() - started, 1)})
        return ocr_pdf.text, "ocr" if ocr_pdf.text else "none", ocr_pdf.page_count or pages, out
    except (OcrError, Exception) as exc:  # noqa: BLE001
        log.warning("OCR failed, uploading without text", extra={"file": path.name, "error": str(exc)})
        return text, source, pages, upload


def ingest_file(svc: Services, path: Path) -> int:
    """Process one ready file. Returns the new document id. The caller deletes the local file."""
    s = svc.settings
    digest = sha256_file(path)
    s.tmp_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="ingest-", dir=s.tmp_dir))
    try:
        text, source, pages, upload_path = extract_text(svc, path, workdir)
        fields = extract_fields(text, s.extraction)
        preds = svc.propose(text, fields.sender)
        name = svc.filename_for(preds[0].label if preds else None, fields.doc_date, fields.sender)

        svc.ensure_review_folder()
        review = s.nextcloud.review_folder
        for _ in range(5):
            remote = svc.nc.unique_path(review, name)
            try:
                entry = svc.nc.upload(upload_path, remote)
                break
            except AlreadyExists:
                continue
        else:
            raise RuntimeError(f"could not find a free name for {name} in review folder")

        with svc.db.session() as sess:
            dup_of = sess.scalar(
                select(Document.id).where(Document.sha256 == digest).order_by(Document.id).limit(1)
            )
            doc = Document(
                sha256=digest,
                duplicate_of=dup_of,
                status=Status.PENDING,
                nc_fileid=entry.fileid,
                nc_path=entry.path,
                nc_etag=entry.etag,
                text=text,
                text_source=source,
                page_count=pages,
                doc_date=fields.doc_date,
                doc_date_guessed=fields.doc_date_guessed,
                sender=fields.sender,
                amount=fields.amount,
                proposed_name=entry.name,
            )
            doc.proposals = [
                Proposal(rank=i, label=p.label, probability=p.probability, source=p.source,
                         model_version=p.model_version)
                for i, p in enumerate(preds)
            ]
            sess.add(doc)
            sess.execute(delete(IngestAttempt).where(IngestAttempt.local_path == str(path)))
            sess.flush()
            doc_id = doc.id
        log.info(
            "document ingested",
            extra={"doc_id": doc_id, "text_source": source, "pages": pages, "duplicate_of": dup_of,
                   "proposal": preds[0].label if preds else None},
        )
        return doc_id
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class IngestWorker:
    """Detects ready files (inotify + polling) and ingests them with bounded concurrency."""

    def __init__(self, svc: Services):
        self.svc = svc
        cfg = svc.settings.watch
        self.dir = cfg.dir
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._seen: dict[Path, tuple[tuple[int, int], float]] = {}
        self._done_undeletable: set[tuple[Path, int, int]] = set()
        self._in_flight: set[Path] = set()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=svc.settings.ingest.concurrency,
                                            thread_name_prefix="ingest")
        self._thread = threading.Thread(target=self._run, name="watcher", daemon=True)
        self._observer = None
        self.last_scan: float | None = None

    # lifecycle ---------------------------------------------------------------------------------
    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.svc.settings.watch.use_inotify:
            self._start_observer()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer is not None:
            self._observer.stop()
        self._thread.join(timeout=10)
        self._executor.shutdown(wait=True, cancel_futures=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def _start_observer(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            worker = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):  # noqa: ANN001
                    worker.wake()

            observer = Observer()
            observer.schedule(Handler(), str(self.dir), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
        except Exception as exc:  # noqa: BLE001
            log.warning("inotify unavailable, using polling only", extra={"error": str(exc)})

    # scanning ----------------------------------------------------------------------------------
    def _matches(self, path: Path) -> bool:
        cfg = self.svc.settings.watch
        rel_parts = path.relative_to(self.dir).parts
        if any(part.startswith(".") for part in rel_parts):
            return False
        name = path.name.lower()
        if any(fnmatch.fnmatch(name, p.lower()) for p in cfg.ignore_patterns):
            return False
        return any(fnmatch.fnmatch(name, p.lower()) for p in cfg.patterns)

    def candidates(self) -> list[Path]:
        try:
            return sorted(p for p in self.dir.rglob("*") if p.is_file() and self._matches(p))
        except OSError as exc:
            log.error("cannot scan watch dir", extra={"error": str(exc)})
            return []

    def scan(self) -> list[Path]:
        """One pass: returns the files that were submitted for ingest."""
        stable = self.svc.settings.watch.stable_seconds
        now = time.monotonic()
        files = self.candidates()
        with self.svc.db.session() as sess:
            attempts = {a.local_path: a for a in sess.scalars(select(IngestAttempt))}
            existing = {str(p) for p in files}
            for path_str, att in attempts.items():
                if path_str not in existing:
                    sess.delete(att)
        submitted = []
        for path in files:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            key = (st.st_size, st.st_mtime_ns)
            prev = self._seen.get(path)
            if prev is None or prev[0] != key:
                self._seen[path] = (key, now)
                if stable > 0:
                    continue
            elif now - prev[1] < stable:
                continue
            if st.st_size == 0 or (path, *key) in self._done_undeletable:
                continue
            att = attempts.get(str(path))
            if att is not None and att.next_try_at > utcnow():
                continue
            if not _readable(path):
                continue
            with self._lock:
                if path in self._in_flight or len(self._in_flight) >= self.svc.settings.ingest.concurrency:
                    continue
                self._in_flight.add(path)
            self._executor.submit(self._process, path, key)
            submitted.append(path)
        for gone in set(self._seen) - set(files):
            self._seen.pop(gone, None)
        self.last_scan = time.time()
        return submitted

    def _run(self) -> None:
        log.info("watching for documents", extra={"dir": str(self.dir)})
        poll = self.svc.settings.watch.poll_interval
        stable = self.svc.settings.watch.stable_seconds
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:  # noqa: BLE001
                log.exception("scan failed")
            waiting = any(p not in self._in_flight for p in self._seen)
            timeout = min(poll, max(stable, 0.5)) if waiting else poll
            self._wake.wait(timeout)
            self._wake.clear()

    def _process(self, path: Path, key: tuple[int, int]) -> None:
        try:
            doc_id = ingest_file(self.svc, path)
            try:
                path.unlink()
            except OSError as exc:
                log.error("ingested but could not delete local file; it will be ignored",
                          extra={"doc_id": doc_id, "file": path.name, "error": str(exc)})
                self._done_undeletable.add((path, *key))
        except Exception as exc:  # noqa: BLE001
            self._record_failure(path, exc)
        finally:
            with self._lock:
                self._in_flight.discard(path)
            self._wake.set()

    def _record_failure(self, path: Path, exc: Exception) -> None:
        backoff = self.svc.settings.ingest.retry_backoff or [60]
        with self.svc.db.session() as sess:
            att = sess.scalar(select(IngestAttempt).where(IngestAttempt.local_path == str(path)))
            if att is None:
                att = IngestAttempt(local_path=str(path), attempts=0)
                sess.add(att)
            att.attempts += 1
            att.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
            delay = backoff[min(att.attempts - 1, len(backoff) - 1)]
            att.next_try_at = utcnow() + timedelta(seconds=delay)
            attempts = att.attempts
        log.warning("ingest failed, will retry",
                    extra={"file": path.name, "attempts": attempts, "retry_in_s": delay, "error": str(exc)})


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False
