"""Filename templating and sanitizing."""

from __future__ import annotations

import re
import string
import unicodedata
from datetime import date

_FORBIDDEN = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_formatter = string.Formatter()


def _render_field(name: str, spec: str, values: dict) -> str:
    value = values.get(name)
    if value is None or value == "":
        return ""
    if isinstance(value, date):
        return value.strftime(spec) if spec else value.isoformat()
    return format(value, spec) if spec else str(value)


def render_template(template: str, values: dict) -> str:
    """Render a ``str.format``-style template. Empty fields are dropped together with the
    separator that would have joined them to their neighbour.

    ``{date:%Y-%m-%d}_{sender}_{title}`` with sender empty → ``2026-01-05_Krankenkasse``.
    """
    prefix = suffix = ""
    out: list[str] = []
    pending_sep: str | None = None
    for i, (literal, field, spec, _conv) in enumerate(_formatter.parse(template)):
        if i == 0:
            prefix = literal
        elif field is None:
            suffix = literal
            break
        elif out and pending_sep is None:
            pending_sep = literal
        if field is None:
            break
        rendered = _render_field(field, spec or "", values)
        if not rendered:
            continue
        if out and pending_sep is not None:
            out.append(pending_sep)
        out.append(rendered)
        pending_sep = None
    if not out:
        return ""
    return prefix + "".join(out) + suffix


def sanitize_filename(name: str, max_length: int = 120, ext: str = ".pdf", fallback: str = "Scan") -> str:
    name = unicodedata.normalize("NFC", name)
    if name.lower().endswith(ext):
        name = name[: -len(ext)]
    name = _FORBIDDEN.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" ._-")
    if not name:
        name = fallback
    limit = max(1, max_length - len(ext))
    if len(name) > limit:
        name = name[:limit].rstrip(" ._-")
    return name + ext


def build_filename(
    template: str,
    *,
    doc_date: date | None,
    sender: str | None,
    title: str | None,
    max_length: int = 120,
    fallback_title: str = "Scan",
) -> str:
    values = {
        "date": doc_date,
        "sender": sender or "",
        "title": title or fallback_title,
        "year": doc_date.year if doc_date else "",
    }
    return sanitize_filename(render_template(template, values), max_length, fallback=fallback_title)
