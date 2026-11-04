"""Filing, deleting, reconciliation and folder operations (server side of the review UI)."""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select

from scansort.db import ArchiveFile, Document, Folder, Status, utcnow
from scansort.labels import resolve
from scansort.naming import sanitize_filename
from scansort.services import Services
from scansort.webdav import AlreadyExists, DavError, is_within, join

log = logging.getLogger(__name__)

_BAD_SEGMENT = re.compile(r'[\\:*?"<>|\x00-\x1f]')


class FilingError(Exception):
    pass


@dataclass
class LabelOption:
    label: str
    doc_count: int


def clean_label(label: str) -> str:
    segs = [s.strip() for s in (label or "").replace("\\", "/").split("/") if s.strip()]
    if not segs:
        raise FilingError("No target folder selected.")
    for seg in segs:
        if seg in (".", "..") or _BAD_SEGMENT.search(seg):
            raise FilingError(f"Invalid folder name: {seg!r}")
    return "/".join(segs)


# --- reconciliation ------------------------------------------------------------------------------
def reconcile_document(svc: Services, doc_id: int) -> Document:
    """Check where a pending document is now (§7.5). Network errors propagate (status unchanged)."""
    with svc.db.session() as sess:
        doc = sess.get(Document, doc_id)
        if doc is None:
            raise FilingError("Document not found.")
        if doc.status != Status.PENDING:
            return doc
        fileid, path = doc.nc_fileid, doc.nc_path
    review = svc.settings.nextcloud.review_folder
    if fileid:
        entry = svc.nc.find_by_fileid(fileid, hints=[p for p in (path, review) if p])
    else:
        entry = svc.nc.stat(path) if path else None

    with svc.db.session() as sess:
        doc = sess.get(Document, doc_id)
        if entry is None:
            doc.status = Status.DELETED_EXTERNALLY
            log.info("document deleted externally", extra={"doc_id": doc_id})
        elif is_within(entry.path, review):
            doc.nc_path, doc.nc_etag = entry.path, entry.etag
        else:
            doc.status = Status.HANDLED_EXTERNALLY
            doc.nc_path = doc.final_path = entry.path
            doc.final_label = svc.labels.label_for_path(posixpath.dirname(entry.path))
            doc.filed_at = utcnow()
            log.info("document handled externally", extra={"doc_id": doc_id, "path": entry.path})
        sess.flush()
        sess.refresh(doc)
        return doc


def reconcile_all(svc: Services) -> int:
    with svc.db.session() as sess:
        ids = sess.scalars(select(Document.id).where(Document.status == Status.PENDING)).all()
    changed = 0
    for doc_id in ids:
        try:
            if reconcile_document(svc, doc_id).status != Status.PENDING:
                changed += 1
        except DavError as exc:
            log.warning("reconciliation aborted", extra={"error": str(exc)})
            break
    svc.db.set_kv("last_reconcile", utcnow().isoformat(timespec="seconds"))
    return changed


def _require_pending(svc: Services, doc_id: int) -> Document:
    doc = reconcile_document(svc, doc_id)
    if doc.status == Status.HANDLED_EXTERNALLY:
        raise FilingError(f"The document was moved in Nextcloud to {doc.nc_path}.")
    if doc.status in (Status.DELETED_EXTERNALLY, Status.DELETED):
        raise FilingError("The document was deleted.")
    if doc.status != Status.PENDING:
        raise FilingError(f"The document is already {doc.status}.")
    return doc


# --- filing --------------------------------------------------------------------------------------
def target_folder(svc: Services, label: str, doc_date: date | None) -> str:
    return svc.labels.absolute(resolve(label, doc_date))


def _move_unique(svc: Services, src: str, folder: str, filename: str) -> str:
    for _ in range(10):
        dst = svc.nc.unique_path(folder, filename)
        try:
            svc.nc.move(src, dst)
            return dst
        except AlreadyExists:
            continue
    raise FilingError("Could not find a free filename in the target folder.")


def file_document(
    svc: Services, doc_id: int, label: str, filename: str | None = None, doc_date: date | None = None
) -> Document:
    label = clean_label(label)
    doc = _require_pending(svc, doc_id)
    doc_date = doc_date or doc.doc_date
    folder = target_folder(svc, label, doc_date)
    if is_within(folder, svc.settings.nextcloud.review_folder):
        raise FilingError("Cannot file into the review folder.")
    n = svc.settings.naming
    name = sanitize_filename(filename or doc.proposed_name, n.max_length, fallback=n.default_title)

    svc.nc.makedirs(folder)
    dst = _move_unique(svc, doc.nc_path, folder, name)
    entry = svc.nc.stat(dst)
    norm_label = svc.labels.label_for_path(folder) or label

    with svc.db.session() as sess:
        doc = sess.get(Document, doc_id)
        if doc_date != doc.doc_date:
            doc.doc_date, doc.doc_date_guessed = doc_date, False
        doc.status = Status.FILED
        doc.final_label = norm_label
        doc.final_path = doc.nc_path = dst
        doc.nc_etag = entry.etag if entry else doc.nc_etag
        doc.filed_at = utcnow()
        if doc.nc_fileid and doc.text and not svc.labels.is_label_excluded(norm_label):
            row = sess.get(ArchiveFile, doc.nc_fileid) or ArchiveFile(nc_fileid=doc.nc_fileid)
            row.nc_path, row.etag, row.label, row.text = dst, doc.nc_etag or "", norm_label, doc.text
            row.synced_at = utcnow()
            sess.merge(row)
        folder_row = sess.get(Folder, folder)
        if folder_row is None:
            sess.add(Folder(path=folder, label=norm_label, doc_count=1, synced_at=utcnow()))
        else:
            folder_row.doc_count += 1
        sess.flush()
        sess.refresh(doc)
    log.info("document filed", extra={"doc_id": doc_id, "label": norm_label, "path": dst})
    return doc


def accept_document(svc: Services, doc_id: int) -> Document:
    """One-click accept: top proposal + proposed name."""
    with svc.db.session() as sess:
        doc = sess.get(Document, doc_id)
        if doc is None:
            raise FilingError("Document not found.")
        top = doc.top_proposal
        if top is None:
            raise FilingError("No proposal for this document; open it and choose a folder.")
        label = top.label
    return file_document(svc, doc_id, label)


def delete_document(svc: Services, doc_id: int) -> Document:
    doc = _require_pending(svc, doc_id)
    trash = svc.settings.nextcloud.trash_folder
    if trash:
        svc.nc.makedirs(trash)
        dst = _move_unique(svc, doc.nc_path, trash, posixpath.basename(doc.nc_path))
    else:
        svc.nc.delete(doc.nc_path)
        dst = None
    with svc.db.session() as sess:
        doc = sess.get(Document, doc_id)
        doc.status = Status.DELETED
        doc.final_path = dst
        doc.filed_at = utcnow()
    log.info("document deleted", extra={"doc_id": doc_id, "trash": bool(trash)})
    return doc


# --- folders -------------------------------------------------------------------------------------
def create_folder(svc: Services, parent_label: str | None, name: str, doc_date: date | None) -> str:
    """MKCOL ``name`` under the (resolved) parent label; returns the new label."""
    name = clean_label(name)
    if "/" in name:
        raise FilingError("Folder name must not contain '/'.")
    parent = target_folder(svc, clean_label(parent_label), doc_date) if parent_label else svc.labels.root
    path = join(parent, name)
    if svc.labels.is_excluded(path):
        raise FilingError("That folder is excluded from sorting.")
    svc.nc.makedirs(path)
    label = svc.labels.label_for_path(path)
    with svc.db.session() as sess:
        if sess.get(Folder, path) is None:
            sess.add(Folder(path=path, label=label, doc_count=0, synced_at=utcnow()))
    log.info("folder created", extra={"path": path})
    return label


def label_options(svc: Services) -> list[LabelOption]:
    with svc.db.session() as sess:
        rows = sess.execute(
            select(Folder.label, func.sum(Folder.doc_count)).group_by(Folder.label).order_by(Folder.label)
        ).all()
    return [LabelOption(lbl, int(n or 0)) for lbl, n in rows if not svc.labels.is_label_excluded(lbl)]


def recent_labels(svc: Services, limit: int = 5) -> list[str]:
    with svc.db.session() as sess:
        rows = sess.execute(
            select(Document.final_label, func.max(Document.filed_at).label("last"))
            .where(Document.status == Status.FILED, Document.final_label.is_not(None))
            .group_by(Document.final_label)
            .order_by(func.max(Document.filed_at).desc())
            .limit(limit)
        ).all()
    return [r[0] for r in rows]
