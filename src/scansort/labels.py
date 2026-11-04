"""Labels are folder paths relative to ``nextcloud.root``, with a trailing year folder normalized
to ``{year}`` (``Steuern/2024`` → ``Steuern/{year}``)."""

from __future__ import annotations

import fnmatch
import re
from datetime import date

from scansort.webdav import is_within, join, normpath

YEAR = "{year}"


class LabelRules:
    def __init__(self, root: str, year_folder_pattern: str, exclude: list[str], excluded_paths: list[str]):
        self.root = normpath(root)
        self.year_re = re.compile(year_folder_pattern)
        self.exclude = exclude
        self.excluded_paths = [normpath(p) for p in excluded_paths if p]

    # path <-> relative ---------------------------------------------------------------------------
    def relative(self, path: str) -> str | None:
        """Absolute user path → path relative to root (``""`` for root itself, None if outside)."""
        path = normpath(path)
        if not is_within(path, self.root):
            return None
        rel = path[len(self.root):] if self.root != "/" else path
        return rel.strip("/")

    def absolute(self, rel: str) -> str:
        return join(self.root, rel)

    # normalization -------------------------------------------------------------------------------
    def normalize(self, rel_folder: str) -> str:
        segs = [s for s in rel_folder.strip("/").split("/") if s]
        if segs and self.year_re.match(segs[-1]):
            segs[-1] = YEAR
        return "/".join(segs)

    def label_for_path(self, folder_path: str) -> str | None:
        rel = self.relative(folder_path)
        if rel is None or rel == "":
            return None
        return self.normalize(rel)

    def is_excluded(self, folder_path: str) -> bool:
        """True if the folder (absolute path) is the review/trash folder or matches an exclude glob."""
        folder_path = normpath(folder_path)
        if any(is_within(folder_path, p) for p in self.excluded_paths):
            return True
        rel = self.relative(folder_path)
        if rel is None:
            return True
        if rel == "":
            return False
        segs = rel.split("/")
        prefixes = ["/".join(segs[: i + 1]) for i in range(len(segs))]
        for pattern in self.exclude:
            pat = pattern.strip("/")
            base = pat[:-3] if pat.endswith("/**") else pat
            for prefix in prefixes:
                if fnmatch.fnmatchcase(prefix, pat) or fnmatch.fnmatchcase(prefix, base):
                    return True
        return False

    def is_label_excluded(self, label: str) -> bool:
        return self.is_excluded(self.absolute(label.replace(YEAR, "0000")))


def has_year(label: str) -> bool:
    return YEAR in label


def resolve(label: str, doc_date: date | None, today: date | None = None) -> str:
    """Replace ``{year}`` with the document year (fallback: current year)."""
    year = (doc_date or today or date.today()).year
    return label.replace(YEAR, f"{year:04d}")


def title_from_label(label: str | None) -> str:
    """Last non-year segment, e.g. ``Versicherung/Krankenkasse`` → ``Krankenkasse``."""
    if not label:
        return ""
    segs = [s for s in label.split("/") if s and s != YEAR]
    return segs[-1] if segs else ""
