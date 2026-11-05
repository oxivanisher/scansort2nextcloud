from datetime import date

import pytest

from scansort.labels import LabelRules, resolve, title_from_label
from scansort.naming import build_filename, render_template, sanitize_filename
from scansort.webdav import collision_candidates

T = "{date:%Y-%m-%d}_{sender}_{title}"


@pytest.mark.parametrize("values,expected", [
    ({"date": date(2026, 1, 5), "sender": "CSS", "title": "Krankenkasse"}, "2026-01-05_CSS_Krankenkasse"),
    ({"date": date(2026, 1, 5), "sender": "", "title": "Krankenkasse"}, "2026-01-05_Krankenkasse"),
    ({"date": None, "sender": "CSS", "title": "Krankenkasse"}, "CSS_Krankenkasse"),
    ({"date": date(2026, 1, 5), "sender": "", "title": ""}, "2026-01-05"),
    ({}, ""),
])
def test_render_template(values, expected):
    assert render_template(T, values) == expected


def test_render_prefix_suffix():
    assert render_template("[{a}]-{b}!", {"a": "", "b": "x"}) == "[x!"
    assert render_template("scan-{a}", {"a": "1"}) == "scan-1"


def test_sanitize():
    assert sanitize_filename('a/b\\c:d*e?"f<g>h|i') == "a-b-c-d-e--f-g-h-i.pdf"
    assert sanitize_filename("  many   spaces  .pdf") == "many spaces.pdf"
    assert sanitize_filename("") == "Scan.pdf"
    long = sanitize_filename("x" * 300, max_length=50)
    assert len(long) == 50 and long.endswith(".pdf")
    assert sanitize_filename("Report.pdf") == "Report.pdf"


def test_build_filename_defaults():
    assert build_filename(T, doc_date=date(2026, 2, 1), sender=None, title=None) == "2026-02-01_Scan.pdf"


def test_collision_candidates():
    gen = collision_candidates("a.pdf")
    assert [next(gen) for _ in range(3)] == ["a.pdf", "a (2).pdf", "a (3).pdf"]


@pytest.fixture
def rules():
    return LabelRules("/Dokumente", r"^\d{4}$", ["_*", "Archiv/Alt/**"], ["/Dokumente/_Review", "/Trash"])


def test_label_normalization(rules):
    assert rules.label_for_path("/Dokumente/Steuern/2024") == "Steuern/{year}"
    assert rules.label_for_path("/Dokumente/Versicherung/Krankenkasse") == "Versicherung/Krankenkasse"
    assert rules.label_for_path("/Dokumente/2024/Foo") == "2024/Foo"
    assert rules.label_for_path("/Dokumente") is None
    assert rules.label_for_path("/Other/x") is None


def test_exclusion(rules):
    assert rules.is_excluded("/Dokumente/_Review")
    assert rules.is_excluded("/Dokumente/_Review/sub")
    assert rules.is_excluded("/Dokumente/_Privat")
    assert rules.is_excluded("/Dokumente/Archiv/Alt")
    assert rules.is_excluded("/Dokumente/Archiv/Alt/2001")
    assert not rules.is_excluded("/Dokumente/Archiv")
    # globs match the path relative to root: "_*" only excludes top-level folders ...
    assert not rules.is_excluded("/Dokumente/Steuern/_x")
    # ... "**/_*" excludes nested ones
    nested = LabelRules("/Dokumente", r"^\d{4}$", ["**/_*"], [])
    assert nested.is_excluded("/Dokumente/Steuern/_x")
    assert rules.is_excluded("/Outside")


def test_default_exclusion_keeps_underscore_folders():
    from scansort.config import ClassifierSettings

    default = LabelRules("/Dokumente", r"^\d{4}$", ClassifierSettings().exclude, ["/Dokumente/_Review", ""])
    assert not default.is_excluded("/Dokumente/_Versicherung/CSS")
    assert default.is_excluded("/Dokumente/_Review")
    assert default.is_excluded("/Dokumente/_Review/sub")


def test_resolve_and_title():
    assert resolve("Steuern/{year}", date(2024, 3, 1)) == "Steuern/2024"
    assert resolve("Steuern/{year}", None, today=date(2026, 1, 1)) == "Steuern/2026"
    assert title_from_label("Steuern/{year}") == "Steuern"
    assert title_from_label("Versicherung/Krankenkasse") == "Krankenkasse"
    assert title_from_label(None) == ""
