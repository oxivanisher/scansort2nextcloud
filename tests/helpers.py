"""Test helpers: minimal PDF writer and an in-memory fake Nextcloud."""

from __future__ import annotations

import itertools
import posixpath
from datetime import UTC, datetime
from pathlib import Path

from scansort.webdav import AlreadyExists, DavEntry, NotFound, collision_candidates, is_within, join, normpath


def make_pdf(lines: list[str] | None = None, pages: int = 1) -> bytes:
    """Build a tiny valid PDF. With ``lines`` the pages get a Helvetica text layer; without, no text."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs: list[bytes] = []
    page_ids = [3 + 2 * i for i in range(pages)]
    font_id = 3 + 2 * pages
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for i in range(pages):
        content = b""
        if lines:
            ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
            for line in lines:
                ops.append(f"({esc(line)}) Tj T*")
            ops.append("ET")
            content = "\n".join(ops).encode("latin-1")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {page_ids[i] + 1} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class FakeNextcloud:
    """In-memory stand-in for NextcloudClient (same public interface)."""

    def __init__(self) -> None:
        self.dirs: set[str] = {"/"}
        self.files: dict[str, bytes] = {}
        self.ids: dict[str, str] = {}
        self.etags: dict[str, str] = {}
        self._ids = itertools.count(1000)
        self.calls: list[tuple] = []
        self.online = True

    # helpers
    def _check(self):
        if not self.online:
            from scansort.webdav import DavError

            raise DavError("connection refused")

    def _new_id(self, path: str) -> None:
        self.ids[path] = str(next(self._ids))
        self.etags[path] = "e" + self.ids[path]

    def add_dir(self, path: str) -> None:
        path = normpath(path)
        while path not in self.dirs:
            self.dirs.add(path)
            self._new_id(path)
            path = posixpath.dirname(path)

    def add_file(self, path: str, data: bytes) -> str:
        path = normpath(path)
        self.add_dir(posixpath.dirname(path))
        self.files[path] = data
        self._new_id(path)
        return self.ids[path]

    def _entry(self, path: str) -> DavEntry:
        is_dir = path in self.dirs
        return DavEntry(
            path=path, is_dir=is_dir, fileid=self.ids.get(path, "1"), etag=self.etags.get(path, "root"),
            size=None if is_dir else len(self.files[path]), last_modified=datetime.now(UTC),
        )

    # interface
    def stat(self, path):
        self._check()
        path = normpath(path)
        if path in self.dirs or path in self.files:
            return self._entry(path)
        return None

    def exists(self, path):
        return self.stat(path) is not None

    def listdir(self, path):
        self._check()
        path = normpath(path)
        if path not in self.dirs:
            raise NotFound(path, 404)
        children = [p for p in self.dirs | set(self.files) if p != path and posixpath.dirname(p) == path]
        return [self._entry(p) for p in sorted(children)]

    def mkcol(self, path):
        self._check()
        path = normpath(path)
        if path in self.dirs or path in self.files:
            raise AlreadyExists(path, 405)
        if posixpath.dirname(path) not in self.dirs:
            from scansort.webdav import DavError

            raise DavError("conflict", 409)
        self.dirs.add(path)
        self._new_id(path)
        self.calls.append(("MKCOL", path))

    def makedirs(self, path):
        path = normpath(path)
        if path == "/" or path in self.dirs:
            return
        self.makedirs(posixpath.dirname(path))
        self.mkcol(path)

    def upload(self, local_path: Path, remote_path: str):
        self._check()
        remote_path = normpath(remote_path)
        if remote_path in self.files:
            raise AlreadyExists(remote_path, 412)
        if posixpath.dirname(remote_path) not in self.dirs:
            raise NotFound(remote_path, 404)
        self.files[remote_path] = Path(local_path).read_bytes()
        self._new_id(remote_path)
        self.calls.append(("PUT", remote_path))
        return self._entry(remote_path)

    def download(self, path):
        self._check()
        path = normpath(path)
        if path not in self.files:
            raise NotFound(path, 404)
        return self.files[path]

    def move(self, src, dst):
        self._check()
        src, dst = normpath(src), normpath(dst)
        if src not in self.files:
            raise NotFound(src, 404)
        if dst in self.files or dst in self.dirs:
            raise AlreadyExists(dst, 412)
        if posixpath.dirname(dst) not in self.dirs:
            from scansort.webdav import DavError

            raise DavError("conflict", 409)
        self.files[dst] = self.files.pop(src)
        self.ids[dst] = self.ids.pop(src)
        self.etags[dst] = self.etags.pop(src)
        self.calls.append(("MOVE", src, dst))

    def delete(self, path):
        self._check()
        path = normpath(path)
        if path not in self.files:
            raise NotFound(path, 404)
        del self.files[path]
        self.ids.pop(path)
        self.calls.append(("DELETE", path))

    def find_by_fileid(self, fileid, hints=()):
        self._check()
        for path, fid in self.ids.items():
            if fid == fileid:
                return self._entry(path)
        return None

    def unique_path(self, folder, filename):
        for cand in collision_candidates(filename):
            path = join(folder, cand)
            if not self.exists(path):
                return path

    def preview(self, fileid, width=256, height=362):
        return None

    def ping(self):
        return self.online

    def close(self):
        pass

    def files_in(self, folder):
        return sorted(p for p in self.files if is_within(p, folder))
