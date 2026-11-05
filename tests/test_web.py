import re

import pytest
from fastapi.testclient import TestClient

from scansort.db import Document, Status
from scansort.ingest import ingest_file
from scansort.web.app import create_app
from tests.conftest import PASSWORD
from tests.helpers import make_pdf


@pytest.fixture
def client(settings, svc):
    app = create_app(settings, services=svc, background=False)
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client):
    page = client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "next": "/",
                                    "csrf_token": csrf_of(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    return csrf_of(client.get("/").text)


def add_doc(svc, settings, lines=("Steuererklärung", "Datum 01.02.2025"), name="s.pdf"):
    p = settings.watch.dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(make_pdf(list(lines)))
    return ingest_file(svc, p)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["db"] is True


def test_icons_public(client):
    r = client.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    for path in ("/static/favicon.svg", "/static/logo.svg", "/static/apple-touch-icon.png"):
        assert client.get(path, follow_redirects=False).status_code == 200, path
    login = client.get("/login").text
    assert 'href="/static/favicon.svg"' in login and 'src="/static/logo.svg"' in login


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/doc/1/suggest", headers={"Accept": "application/json"}).status_code == 401


def test_bad_login_and_throttle(client):
    token = csrf_of(client.get("/login").text)
    codes = [client.post("/login", data={"username": "admin", "password": "nope", "csrf_token": token}).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and 429 in codes


SPECIAL_PW = 'Sup3r$ecret!"#%&/()=?`´\\\'<>{}[]~^ äöü€ 密码 🔑'


def test_login_with_special_characters(settings, svc):
    from pydantic import SecretStr

    from scansort.web.auth import hash_password

    settings.webui.password_hash = SecretStr(hash_password(SPECIAL_PW))
    with TestClient(create_app(settings, services=svc, background=False), base_url="http://testserver") as c:
        token = csrf_of(c.get("/login").text)
        r = c.post("/login", data={"username": "admin", "password": SPECIAL_PW, "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303


def test_failed_login_logs_reason(client, caplog):
    token = csrf_of(client.get("/login").text)
    with caplog.at_level("WARNING"):
        client.post("/login", data={"username": "root", "password": PASSWORD, "csrf_token": token})
        client.post("/login", data={"username": "admin", "password": "nope", "csrf_token": token})
    text = caplog.text
    assert "unknown username 'root'" in text and "wrong password" in text
    assert PASSWORD not in text and "nope" not in text


@pytest.mark.parametrize("bad", [
    "argon2id=19,t=3,p=4ZUhGvL0Jz9k",  # what Compose leaves after eating the $... parts
    "not-a-hash",
])
def test_mangled_hash_refused(settings, svc, bad):
    from pydantic import SecretStr

    settings.webui.password_hash = SecretStr(bad)
    with pytest.raises(RuntimeError, match="not a valid argon2 hash"):
        create_app(settings, services=svc, background=False)


def test_session_cookie_flags(client):
    r = client.get("/login")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie


def test_csrf_rejected(client, svc, settings):
    login(client)
    doc_id = add_doc(svc, settings)
    r = client.post(f"/doc/{doc_id}/accept", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    r = client.post(f"/doc/{doc_id}/accept")
    assert r.status_code == 403


def test_queue_and_accept_htmx(client, svc, settings):
    token = login(client)
    doc_id = add_doc(svc, settings)
    page = client.get("/")
    assert "20250201_Steuern.pdf" in page.text and "Steuern/2025" in page.text
    r = client.post(f"/doc/{doc_id}/accept", headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert r.status_code == 200 and "Filed to /Dokumente/Steuern/2025/20250201_Steuern.pdf" in r.text
    with svc.db.session() as s:
        assert s.get(Document, doc_id).status == Status.FILED


def test_detail_file_and_pdf(client, svc, settings, nc):
    token = login(client)
    first = add_doc(svc, settings, name="a.pdf")
    second = add_doc(svc, settings, ("Anderes", "Datum 03.03.2024"), name="b.pdf")
    page = client.get(f"/doc/{first}")
    assert page.status_code == 200 and f'data-next="/doc/{second}"' in page.text
    pdf = client.get(f"/doc/{first}/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")

    sug = client.get(f"/doc/{second}/suggest", params={"label": "Briefe/{year}", "date": "2024-03-03"}).json()
    assert sug == {"filename": "20240303_Briefe.pdf", "target": "/Dokumente/Briefe/2024"}

    r = client.post(f"/doc/{first}/file", data={"csrf_token": token, "label": "Privat/Briefe",
                                                "filename": "Mein Brief", "doc_date": "2025-02-01"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/doc/{second}"
    assert "/Dokumente/Privat/Briefe/Mein Brief.pdf" in nc.files

    r = client.post("/folders/new", data={"parent": "Privat", "name": "Neu", "doc_date": ""},
                    headers={"X-CSRF-Token": token})
    assert r.json()["label"] == "Privat/Neu"

    r = client.post(f"/doc/{second}/delete", data={"csrf_token": token}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_status_page(client):
    login(client)
    r = client.get("/status")
    assert r.status_code == 200 and "Classifier" in r.text
    assert "test-secret" not in r.text and "$argon2" not in r.text


def test_requires_password_hash(settings):
    from pydantic import SecretStr

    settings.webui.password_hash = SecretStr("")
    with pytest.raises(RuntimeError, match="hash-password"):
        create_app(settings, background=False)


def test_date_format_parsing_and_display(client, svc, settings):
    from datetime import date

    from scansort.web.app import _parse_date, date_placeholder

    assert _parse_date("22.01.2026") == date(2026, 1, 22)
    assert _parse_date("2.1.26") == date(2026, 1, 2)
    assert _parse_date("2026-01-22") == date(2026, 1, 22)  # ISO always works
    assert _parse_date("01/22/2026", "%m/%d/%Y") == date(2026, 1, 22)
    assert _parse_date("32.01.2026") is None and _parse_date("") is None
    assert date_placeholder("%d.%m.%Y") == "dd.mm.yyyy"

    token = login(client)
    doc_id = add_doc(svc, settings)
    assert "01.02.2025" in client.get("/").text
    page = client.get(f"/doc/{doc_id}").text
    assert 'value="01.02.2025"' in page and 'placeholder="dd.mm.yyyy"' in page
    sug = client.get(f"/doc/{doc_id}/suggest", params={"label": "Briefe/{year}", "date": "03.03.2024"}).json()
    assert sug["target"] == "/Dokumente/Briefe/2024"

    r = client.post(f"/doc/{doc_id}/file", data={"csrf_token": token, "label": "Privat/Briefe",
                                               "filename": "x", "doc_date": "31.02.2025"},
                    follow_redirects=True)
    assert "Invalid date" in r.text
    with svc.db.session() as s:
        assert s.get(Document, doc_id).status == Status.PENDING
