from datetime import date

import pytest

from scansort.archive import refresh_folders, sync_and_train, sync_archive
from scansort.db import ArchiveFile, Document, Folder, Status
from scansort.filing import (
    FilingError,
    accept_document,
    create_folder,
    delete_document,
    file_document,
    label_options,
    recent_labels,
    reconcile_all,
)
from scansort.ingest import ingest_file
from tests.helpers import make_pdf

ARCHIVE = {
    "/Dokumente/Steuern/2023/a.pdf": ["Steuererklärung Einkommen Vermögen Veranlagung Kantonssteuer"],
    "/Dokumente/Steuern/2024/b.pdf": ["Steuerverwaltung Steuerrechnung Bundessteuer Einkommen"],
    "/Dokumente/Telefon/Swisscom/c.pdf": ["Swisscom Rechnung Mobile Abo Internet"],
    "/Dokumente/Telefon/Swisscom/d.pdf": ["Swisscom Mobile Roaming Abonnement Rechnung"],
    "/Dokumente/Versicherung/e.pdf": ["Einzeldokument Versicherung Police"],
    "/Dokumente/_Privat/secret.pdf": ["Geheim Geheim"],
    "/Dokumente/root.pdf": ["Liegt im Root"],
}


@pytest.fixture
def archive(nc):
    for path, lines in ARCHIVE.items():
        nc.add_file(path, make_pdf(lines))
    nc.add_dir("/Dokumente/Leer")
    return nc


def ingest_text(svc, settings, lines, name="scan.pdf"):
    p = settings.watch.dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(make_pdf(lines))
    return ingest_file(svc, p)


def test_sync_archive(svc, archive):
    stats = sync_archive(svc)
    assert stats.files == 5 and stats.downloaded == 5
    with svc.db.session() as s:
        labels = sorted(a.label for a in s.query(ArchiveFile))
        folders = {f.path: (f.label, f.doc_count) for f in s.query(Folder)}
    assert labels == ["Steuern/{year}", "Steuern/{year}", "Telefon/Swisscom", "Telefon/Swisscom", "Versicherung"]
    assert folders["/Dokumente/Leer"] == ("Leer", 0)
    assert folders["/Dokumente/Steuern/2024"] == ("Steuern/{year}", 1)
    assert not any("_Privat" in p or "_Review" in p for p in folders)

    # second run: nothing downloaded; deleted file removed from cache
    del archive.files["/Dokumente/Versicherung/e.pdf"]
    stats = sync_archive(svc)
    assert stats.downloaded == 0 and stats.unchanged == 4 and stats.removed == 1


def test_sync_and_train_then_propose_and_accept(svc, archive, settings):
    doc_id = ingest_text(svc, settings, ["Swisscom Mobile Rechnung", "Datum 05.01.2025"])
    with svc.db.session() as s:
        assert s.get(Document, doc_id).proposals == []  # cold start
    assert sync_and_train(svc)
    assert svc.model.info.labels == 2  # "Versicherung" has only 1 sample
    with svc.db.session() as s:
        doc = s.get(Document, doc_id)
        assert doc.top_proposal.label == "Telefon/Swisscom"  # re-proposed after training

    doc = accept_document(svc, doc_id)
    assert doc.status == Status.FILED
    assert doc.final_path == "/Dokumente/Telefon/Swisscom/2025-01-05_Swisscom_Scan.pdf"
    assert doc.final_path in archive.files
    assert recent_labels(svc) == ["Telefon/Swisscom"]
    with svc.db.session() as s:
        assert s.get(ArchiveFile, doc.nc_fileid).label == "Telefon/Swisscom"


def test_file_with_year_and_collision(svc, archive, settings):
    archive.add_file("/Dokumente/Steuern/2022/x.pdf", b"%PDF")
    doc_id = ingest_text(svc, settings, ["Brief", "Datum 01.06.2022"])
    doc = file_document(svc, doc_id, "Steuern/{year}", "x.pdf")
    assert doc.final_path == "/Dokumente/Steuern/2022/x (2).pdf"
    assert doc.final_label == "Steuern/{year}"

    doc2 = ingest_text(svc, settings, ["Brief"], name="b.pdf")
    filed = file_document(svc, doc2, "Steuern/{year}", "y.pdf", date(2019, 1, 1))
    assert filed.final_path == "/Dokumente/Steuern/2019/y.pdf"  # year folder created
    assert filed.doc_date == date(2019, 1, 1)


def test_file_rejects_bad_labels(svc, settings):
    doc_id = ingest_text(svc, settings, ["x"])
    for bad in ["", "../etc", "_Review", "a/./b"]:
        with pytest.raises(FilingError):
            file_document(svc, doc_id, bad)


def test_reconcile(svc, nc, settings):
    moved = ingest_text(svc, settings, ["a"], "a.pdf")
    deleted = ingest_text(svc, settings, ["b"], "b.pdf")
    kept = ingest_text(svc, settings, ["c"], "c.pdf")
    with svc.db.session() as s:
        pm, pd = s.get(Document, moved).nc_path, s.get(Document, deleted).nc_path
    nc.add_dir("/Dokumente/Manual")
    nc.move(pm, "/Dokumente/Manual/a.pdf")
    nc.delete(pd)
    assert reconcile_all(svc) == 2
    with svc.db.session() as s:
        m = s.get(Document, moved)
        assert m.status == Status.HANDLED_EXTERNALLY and m.final_label == "Manual"
        assert s.get(Document, deleted).status == Status.DELETED_EXTERNALLY
        assert s.get(Document, kept).status == Status.PENDING
    with pytest.raises(FilingError, match="moved"):
        file_document(svc, moved, "Somewhere")


def test_reconcile_offline_changes_nothing(svc, nc, settings):
    doc_id = ingest_text(svc, settings, ["a"])
    nc.online = False
    assert reconcile_all(svc) == 0
    with svc.db.session() as s:
        assert s.get(Document, doc_id).status == Status.PENDING


def test_delete_uses_trash_folder(svc, nc, settings):
    settings.nextcloud.trash_folder = "/Trash"
    doc_id = ingest_text(svc, settings, ["a"])
    doc = delete_document(svc, doc_id)
    assert doc.status == Status.DELETED and doc.final_path.startswith("/Trash/")
    settings.nextcloud.trash_folder = None
    doc2 = ingest_text(svc, settings, ["b"], "b.pdf")
    delete_document(svc, doc2)
    assert nc.calls[-1][0] == "DELETE"


def test_create_folder(svc, archive):
    refresh_folders(svc)
    label = create_folder(svc, "Steuern/{year}", "Belege", date(2024, 5, 1))
    assert label == "Steuern/2024/Belege"
    assert "/Dokumente/Steuern/2024/Belege" in archive.dirs
    assert "Steuern/2024/Belege" in [o.label for o in label_options(svc)]
    with pytest.raises(FilingError):
        create_folder(svc, None, "_Hidden", None)
