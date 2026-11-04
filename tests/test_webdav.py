import httpx
import pytest
import respx

from scansort.webdav import AlreadyExists, DavError, NextcloudClient

BASE = "https://cloud.test/remote.php/dav/files/scan%20bot"


def ms(*responses: str) -> str:
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">{"".join(responses)}</d:multistatus>'


def resp(href, fileid, is_dir=False, etag="abc", size=10):
    rtype = "<d:collection/>" if is_dir else ""
    return f"""<d:response><d:href>{href}</d:href><d:propstat><d:prop>
      <d:resourcetype>{rtype}</d:resourcetype><d:getetag>"{etag}"</d:getetag>
      <d:getcontentlength>{size}</d:getcontentlength><oc:fileid>{fileid}</oc:fileid>
      <d:getlastmodified>Tue, 03 Nov 2026 10:00:00 GMT</d:getlastmodified>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""


@pytest.fixture
def client():
    c = NextcloudClient("https://cloud.test/", "scan bot", "secret", retries=0)
    yield c
    c.close()


@respx.mock
def test_listdir_parses_and_decodes(client):
    route = respx.request("PROPFIND", f"{BASE}/Dokumente/St%C3%BCcke/").mock(return_value=httpx.Response(207, text=ms(
        resp("/remote.php/dav/files/scan%20bot/Dokumente/St%C3%BCcke/", "1", True),
        resp("/remote.php/dav/files/scan%20bot/Dokumente/St%C3%BCcke/2024/", "2", True),
        resp("/remote.php/dav/files/scan%20bot/Dokumente/St%C3%BCcke/a%20b.pdf", "3"),
    )))
    entries = client.listdir("/Dokumente/Stücke")
    assert route.calls[0].request.headers["Depth"] == "1"
    assert [(e.path, e.is_dir, e.fileid, e.etag) for e in entries] == [
        ("/Dokumente/Stücke/2024", True, "2", "abc"),
        ("/Dokumente/Stücke/a b.pdf", False, "3", "abc"),
    ]
    assert entries[1].size == 10 and entries[1].last_modified.year == 2026


@respx.mock
def test_stat_not_found(client):
    respx.request("PROPFIND", f"{BASE}/missing.pdf").mock(return_value=httpx.Response(404))
    assert client.stat("/missing.pdf") is None


@respx.mock
def test_move_no_overwrite(client):
    route = respx.request("MOVE", f"{BASE}/a.pdf").mock(return_value=httpx.Response(412))
    with pytest.raises(AlreadyExists):
        client.move("/a.pdf", "/b/c d.pdf")
    req = route.calls[0].request
    assert req.headers["Overwrite"] == "F"
    assert req.headers["Destination"] == f"{BASE}/b/c%20d.pdf"


@respx.mock
def test_makedirs_creates_missing(client):
    respx.request("PROPFIND", f"{BASE}/A").mock(return_value=httpx.Response(207, text=ms(
        resp("/remote.php/dav/files/scan%20bot/A/", "1", True))))
    respx.request("PROPFIND", f"{BASE}/A/B").mock(return_value=httpx.Response(404))
    respx.request("PROPFIND", f"{BASE}/A/B/C").mock(return_value=httpx.Response(404))
    mk = respx.request("MKCOL", url__regex=rf"{BASE}/A/B(/C)?$").mock(return_value=httpx.Response(201))
    client.makedirs("/A/B/C")
    assert [str(c.request.url) for c in mk.calls] == [f"{BASE}/A/B", f"{BASE}/A/B/C"]


@respx.mock
def test_upload_put_then_stat(client, tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    put = respx.put(f"{BASE}/R/x.pdf").mock(return_value=httpx.Response(201))
    respx.request("PROPFIND", f"{BASE}/R/x.pdf").mock(return_value=httpx.Response(207, text=ms(
        resp("/remote.php/dav/files/scan%20bot/R/x.pdf", "77"))))
    entry = client.upload(f, "/R/x.pdf")
    assert entry.fileid == "77"
    assert put.calls[0].request.headers["If-None-Match"] == "*"
    assert put.calls[0].request.headers["Authorization"].startswith("Basic ")


@respx.mock
def test_unique_path(client):
    respx.request("PROPFIND", f"{BASE}/R/a.pdf").mock(return_value=httpx.Response(207, text=ms(
        resp("/remote.php/dav/files/scan%20bot/R/a.pdf", "1"))))
    respx.request("PROPFIND", f"{BASE}/R/a%20%282%29.pdf").mock(return_value=httpx.Response(404))
    assert client.unique_path("/R", "a.pdf") == "/R/a (2).pdf"


@respx.mock
def test_find_by_fileid_search_and_fallback(client):
    search = respx.request("SEARCH", "https://cloud.test/remote.php/dav/").mock(
        return_value=httpx.Response(207, text=ms(resp("/remote.php/dav/files/scan%20bot/Moved/a.pdf", "42"))))
    assert client.find_by_fileid("42").path == "/Moved/a.pdf"
    assert b"<d:literal>42</d:literal>" in search.calls[0].request.content

    search.mock(return_value=httpx.Response(501))
    respx.request("PROPFIND", f"{BASE}/R/a.pdf").mock(return_value=httpx.Response(207, text=ms(
        resp("/remote.php/dav/files/scan%20bot/R/a.pdf", "42"))))
    assert client.find_by_fileid("42", hints=["/R/a.pdf"]).path == "/R/a.pdf"


@respx.mock
def test_network_error_is_daverror(client):
    respx.request("PROPFIND", f"{BASE}/").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(DavError) as ei:
        client.propfind("/")
    assert ei.value.status is None
    assert "secret" not in str(ei.value)
    assert client.ping() is False
