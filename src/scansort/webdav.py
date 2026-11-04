"""Minimal Nextcloud WebDAV client (PROPFIND, GET, PUT, MOVE, MKCOL, DELETE, SEARCH).

All paths are *user paths* as seen by the machine account, always starting with ``/``,
e.g. ``/Dokumente/_Review/2026-01-01_Scan.pdf``.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

log = logging.getLogger(__name__)

NS = {"d": "DAV:", "oc": "http://owncloud.org/ns", "nc": "http://nextcloud.org/ns"}

_PROPS = """<d:prop>
    <d:resourcetype/><d:getetag/><d:getcontentlength/><d:getlastmodified/><d:getcontenttype/>
    <oc:fileid/>
  </d:prop>"""

PROPFIND_BODY = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  {_PROPS}
</d:propfind>"""


class DavError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotFound(DavError):
    pass


class AlreadyExists(DavError):
    pass


@dataclass(frozen=True)
class DavEntry:
    path: str
    is_dir: bool
    fileid: str | None = None
    etag: str | None = None
    size: int | None = None
    last_modified: datetime | None = None
    content_type: str | None = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


def normpath(path: str) -> str:
    path = "/" + path.strip().strip("/")
    path = re.sub(r"/+", "/", path)
    return posixpath.normpath(path) if path != "/" else "/"


def join(*parts: str) -> str:
    return normpath("/".join(p.strip("/") for p in parts if p and p.strip("/")))


def is_within(path: str, folder: str) -> bool:
    path, folder = normpath(path), normpath(folder)
    return folder == "/" or path == folder or path.startswith(folder + "/")


def split_ext(name: str) -> tuple[str, str]:
    stem, ext = posixpath.splitext(name)
    return stem, ext


def collision_candidates(filename: str) -> Iterator[str]:
    """``a.pdf``, ``a (2).pdf``, ``a (3).pdf``, …"""
    stem, ext = split_ext(filename)
    yield filename
    n = 2
    while True:
        yield f"{stem} ({n}){ext}"
        n += 1


class NextcloudClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self.user = user
        self.dav_root = f"{self.url}/remote.php/dav"
        self.files_base = f"{self.dav_root}/files/{quote(user, safe='')}"
        self._files_prefix = unquote(urlparse(self.files_base).path).rstrip("/")
        self._client = httpx.Client(
            auth=(user, password),
            timeout=timeout,
            verify=verify_tls,
            transport=transport or httpx.HTTPTransport(retries=retries, verify=verify_tls),
            headers={"User-Agent": "scansort", "OCS-APIRequest": "true"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    # --- helpers --------------------------------------------------------------------------------
    def href(self, path: str) -> str:
        path = normpath(path)
        encoded = "/".join(quote(seg, safe="") for seg in path.split("/") if seg)
        return f"{self.files_base}/{encoded}"

    def _path_from_href(self, href: str) -> str:
        p = unquote(urlparse(href).path)
        if not p.startswith(self._files_prefix):
            raise DavError(f"unexpected href in response: {href}")
        return normpath(p[len(self._files_prefix):])

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise DavError(f"{method} failed: {exc.__class__.__name__}: {exc}") from exc

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> None:
        if resp.status_code == 404:
            raise NotFound(f"{what}: not found", 404)
        if resp.status_code in (405, 412) and what.startswith(("MKCOL", "MOVE", "PUT")):
            raise AlreadyExists(f"{what}: already exists ({resp.status_code})", resp.status_code)
        if resp.status_code >= 400:
            raise DavError(f"{what}: HTTP {resp.status_code}", resp.status_code)

    def _parse_multistatus(self, content: bytes) -> list[DavEntry]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise DavError(f"invalid XML response: {exc}") from exc
        entries: list[DavEntry] = []
        for resp in root.findall("d:response", NS):
            href = resp.findtext("d:href", default="", namespaces=NS)
            prop = None
            for ps in resp.findall("d:propstat", NS):
                status = ps.findtext("d:status", default="", namespaces=NS)
                if " 200" in status:
                    prop = ps.find("d:prop", NS)
                    break
            if prop is None:
                continue
            rtype = prop.find("d:resourcetype", NS)
            is_dir = rtype is not None and rtype.find("d:collection", NS) is not None
            size_txt = prop.findtext("d:getcontentlength", default=None, namespaces=NS)
            lm_txt = prop.findtext("d:getlastmodified", default=None, namespaces=NS)
            try:
                lm = parsedate_to_datetime(lm_txt) if lm_txt else None
            except (TypeError, ValueError):
                lm = None
            entries.append(
                DavEntry(
                    path=self._path_from_href(href),
                    is_dir=is_dir,
                    fileid=prop.findtext("oc:fileid", default=None, namespaces=NS),
                    etag=(prop.findtext("d:getetag", default="", namespaces=NS) or "").strip('"') or None,
                    size=int(size_txt) if size_txt and size_txt.isdigit() else None,
                    last_modified=lm,
                    content_type=prop.findtext("d:getcontenttype", default=None, namespaces=NS),
                )
            )
        return entries

    # --- WebDAV verbs ---------------------------------------------------------------------------
    def propfind(self, path: str, depth: int = 0) -> list[DavEntry]:
        url = self.href(path)
        if depth == 1 and not url.endswith("/"):
            url += "/"
        resp = self._request(
            "PROPFIND",
            url,
            content=PROPFIND_BODY.encode(),
            headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
        )
        self._raise_for(resp, f"PROPFIND {path}")
        if resp.status_code != 207:
            raise DavError(f"PROPFIND {path}: unexpected HTTP {resp.status_code}", resp.status_code)
        return self._parse_multistatus(resp.content)

    def stat(self, path: str) -> DavEntry | None:
        try:
            entries = self.propfind(path, depth=0)
        except NotFound:
            return None
        return entries[0] if entries else None

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def listdir(self, path: str) -> list[DavEntry]:
        path = normpath(path)
        return [e for e in self.propfind(path, depth=1) if e.path != path]

    def mkcol(self, path: str) -> None:
        resp = self._request("MKCOL", self.href(path))
        self._raise_for(resp, f"MKCOL {path}")

    def makedirs(self, path: str) -> None:
        """Create ``path`` and all missing parents (like ``mkdir -p``)."""
        path = normpath(path)
        if path == "/":
            return
        if self.exists(path):
            return
        self.makedirs(posixpath.dirname(path))
        with contextlib.suppress(AlreadyExists):
            self.mkcol(path)

    def upload(self, local_path: Path, remote_path: str) -> DavEntry:
        """PUT a file without overwriting; returns the new entry (with fileid/etag)."""
        with open(local_path, "rb") as fh:
            resp = self._request(
                "PUT",
                self.href(remote_path),
                content=fh,
                headers={"If-None-Match": "*", "Content-Type": "application/pdf"},
            )
        self._raise_for(resp, f"PUT {remote_path}")
        entry = self.stat(remote_path)
        if entry is None:
            raise DavError(f"PUT {remote_path}: file missing after upload")
        return entry

    def download(self, path: str) -> bytes:
        resp = self._request("GET", self.href(path))
        self._raise_for(resp, f"GET {path}")
        return resp.content

    def stream(self, path: str, chunk_size: int = 65536) -> Iterator[bytes]:
        try:
            with self._client.stream("GET", self.href(path)) as resp:
                self._raise_for(resp, f"GET {path}")
                yield from resp.iter_bytes(chunk_size)
        except httpx.HTTPError as exc:
            raise DavError(f"GET {path} failed: {exc}") from exc

    def move(self, src: str, dst: str) -> None:
        resp = self._request(
            "MOVE", self.href(src), headers={"Destination": self.href(dst), "Overwrite": "F"}
        )
        self._raise_for(resp, f"MOVE {src} -> {dst}")

    def delete(self, path: str) -> None:
        resp = self._request("DELETE", self.href(path))
        self._raise_for(resp, f"DELETE {path}")

    def search_fileid(self, fileid: str) -> DavEntry | None:
        """Nextcloud SEARCH by ``oc:fileid``. Raises DavError if SEARCH is unsupported."""
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:basicsearch>
    <d:select>{_PROPS}</d:select>
    <d:from><d:scope><d:href>/files/{quote(self.user, safe='')}</d:href><d:depth>infinity</d:depth></d:scope></d:from>
    <d:where><d:eq><d:prop><oc:fileid/></d:prop><d:literal>{int(fileid)}</d:literal></d:eq></d:where>
    <d:orderby/>
  </d:basicsearch>
</d:searchrequest>"""
        resp = self._request(
            "SEARCH", self.dav_root + "/", content=body.encode(),
            headers={"Content-Type": "text/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            raise DavError(f"SEARCH fileid {fileid}: HTTP {resp.status_code}", resp.status_code)
        for entry in self._parse_multistatus(resp.content):
            if entry.fileid == str(fileid):
                return entry
        return None

    def find_by_fileid(self, fileid: str, hints: list[str] = ()) -> DavEntry | None:
        """Locate a file by ID: SEARCH, then the hint paths (stat + compare id), then hint folders."""
        try:
            return self.search_fileid(fileid)
        except DavError as exc:
            if exc.status is None:  # network problem: don't guess
                raise
            log.debug("SEARCH unsupported, falling back", extra={"status": exc.status})
        for hint in hints:
            entry = self.stat(hint)
            if entry is None:
                continue
            if entry.fileid == fileid:
                return entry
            if entry.is_dir:
                for child in self.listdir(hint):
                    if child.fileid == fileid:
                        return child
        return None

    def unique_path(self, folder: str, filename: str) -> str:
        for candidate in collision_candidates(filename):
            path = join(folder, candidate)
            if not self.exists(path):
                return path
        raise AssertionError("unreachable")

    def preview(self, fileid: str, width: int = 256, height: int = 362) -> tuple[bytes, str] | None:
        try:
            resp = self._client.get(
                f"{self.url}/index.php/core/preview",
                params={"fileId": fileid, "x": width, "y": height, "a": "1", "mode": "cover"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return resp.content, resp.headers.get("content-type", "image/png")

    def ping(self) -> bool:
        try:
            self.propfind("/", depth=0)
            return True
        except DavError:
            return False
