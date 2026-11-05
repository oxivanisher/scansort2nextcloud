import time
from datetime import date

from sqlalchemy import select, text

from scansort.db import Base, Document, IngestAttempt, Status
from scansort.ingest import IngestWorker, ingest_file
from tests.helpers import make_pdf


def write_pdf(path, lines=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_pdf(lines))
    return path


def test_migrations_match_models(svc):
    with svc.db.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table.name})"))}
            assert cols == {c.name for c in table.columns}, table.name
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_ingest_text_layer(svc, nc, settings):
    src = write_pdf(settings.watch.dir / "scan0001.pdf",
                    ["Swisscom (Schweiz) AG", "Rechnungsdatum: 14.03.2025", "Total CHF 89.90"])
    doc_id = ingest_file(svc, src)
    with svc.db.session() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == Status.PENDING
        assert doc.text_source == "layer" and "Swisscom" in doc.text
        assert doc.doc_date == date(2025, 3, 14) and not doc.doc_date_guessed
        assert doc.sender == "Swisscom" and doc.amount == "CHF 89.90"
        assert doc.proposed_name == "20250314_Scan.pdf"
        assert doc.nc_path == "/Dokumente/_Review/20250314_Scan.pdf"
        assert doc.nc_fileid == nc.ids[doc.nc_path]
    assert src.exists()  # deletion is the worker's job


def test_ingest_rule_proposal_and_duplicates(svc, nc, settings):
    lines = ["Steuererklärung 2024", "Datum 02.02.2025"]
    a = ingest_file(svc, write_pdf(settings.watch.dir / "a.pdf", lines))
    b = ingest_file(svc, write_pdf(settings.watch.dir / "b.pdf", lines))
    with svc.db.session() as s:
        da, db_ = s.get(Document, a), s.get(Document, b)
        assert da.top_proposal.label == "Steuern/{year}" and da.top_proposal.source == "rule"
        assert da.proposed_name == "20250202_Steuern.pdf"
        assert db_.duplicate_of == a
        assert db_.nc_path == "/Dokumente/_Review/20250202_Steuern (2).pdf"


def test_ingest_no_text_without_ocr(svc, settings):
    doc_id = ingest_file(svc, write_pdf(settings.watch.dir / "img.pdf", None))
    with svc.db.session() as s:
        doc = s.get(Document, doc_id)
        assert doc.text_source == "none" and doc.doc_date_guessed
        assert doc.proposals == []


def test_worker_ingests_and_deletes(svc, nc, settings):
    worker = IngestWorker(svc)
    write_pdf(settings.watch.dir / "sub" / "x.pdf", ["Hello", "Datum 01.01.2025"])
    (settings.watch.dir / "y.pdf.part").write_bytes(b"partial")
    (settings.watch.dir / ".hidden.pdf").write_bytes(make_pdf(["x"]))
    assert worker.scan() == [settings.watch.dir / "sub" / "x.pdf"]
    worker._executor.shutdown(wait=True)
    assert not (settings.watch.dir / "sub" / "x.pdf").exists()
    assert (settings.watch.dir / "y.pdf.part").exists()
    assert len(nc.files_in("/Dokumente/_Review")) == 1


def test_worker_stability_check(svc, settings):
    settings.watch.stable_seconds = 0.3
    worker = IngestWorker(svc)
    write_pdf(settings.watch.dir / "x.pdf", ["Hello"])
    assert worker.scan() == []  # first sighting
    assert worker.scan() == []  # not yet stable
    time.sleep(0.35)
    assert len(worker.scan()) == 1
    worker._executor.shutdown(wait=True)


def test_worker_failure_keeps_file_and_backs_off(svc, nc, settings):
    nc.online = False
    worker = IngestWorker(svc)
    src = write_pdf(settings.watch.dir / "x.pdf", ["Hello"])
    worker.scan()
    worker._executor.shutdown(wait=True)
    assert src.exists()
    with svc.db.session() as s:
        att = s.scalar(select(IngestAttempt))
        assert att.attempts == 1 and "connection refused" in att.last_error
    worker2 = IngestWorker(svc)
    worker2._seen = dict(worker._seen)
    assert worker2.scan() == []  # backoff not yet elapsed
    worker2._executor.shutdown(wait=True)


def test_worker_thread_end_to_end(svc, nc, settings):
    worker = IngestWorker(svc)
    worker.start()
    try:
        write_pdf(settings.watch.dir / "x.pdf", ["Hello"])
        deadline = time.time() + 5
        while time.time() < deadline and not nc.files_in("/Dokumente/_Review"):
            time.sleep(0.05)
        assert worker.running
        assert nc.files_in("/Dokumente/_Review")
    finally:
        worker.stop()
