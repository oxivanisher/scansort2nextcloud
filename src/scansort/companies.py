"""Company folders: the direct subfolders of ``classifier.company_folders`` (e.g. ``_Companies/Salt``).

A document is matched to a company by its sender (``extraction.known_senders``) or, failing that, by
company folder names that appear in its text. The match adds a proposal for that company's folder
and, for a known sender without a folder, a proposal for a new ``new_company_folder/<sender>``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from scansort.classifier import Prediction

# legal-form suffixes that letters often omit or spell differently
_LEGAL = re.compile(
    r"(?:\s+|,\s*)(?:ag|gmbh|sa|s\.a\.|sàrl|sarl|ltd\.?|inc\.?|kg|gmbh\s*&\s*co\.?\s*kg|& co\.?|genossenschaft)$",
    re.IGNORECASE,
)
_PARENS = re.compile(r"\s*\([^)]*\)")
_BAD_SEGMENT = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
MIN_NAME_CHARS = 3
TEXT_WINDOW = 3000  # letterheads are at the top; ignore names that only show up deep in the text
CONFIDENT = 0.5  # a model prediction above this outranks a company match elsewhere


def core_name(name: str) -> str:
    """``EWK Herzogenbuchsee AG`` → ``EWK Herzogenbuchsee``; ``Sparkonto (Hanna)`` → ``Sparkonto``."""
    name = _PARENS.sub("", name).strip()
    while True:
        stripped = _LEGAL.sub("", name).strip()
        if stripped == name or not stripped:
            return name
        name = stripped


def _norm(name: str) -> list[str]:
    return core_name(name).casefold().split()


def same_company(a: str, b: str) -> bool:
    """Equal names, or one is a word-wise prefix of the other (``EWK`` ~ ``EWK Herzogenbuchsee AG``)."""
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return False
    n = min(len(ta), len(tb))
    return ta[:n] == tb[:n]


def _text_patterns(name: str) -> list[re.Pattern]:
    core = core_name(name)
    out = []
    if len(core) >= MIN_NAME_CHARS:
        words = r"\s+".join(re.escape(w) for w in core.split())
        out.append(re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE))
    first = core.split()[0] if core.split() else ""
    # an acronym as first word ("EWK Hezogenbuchsee AG") identifies the company on its own
    if first != core and len(first) >= MIN_NAME_CHARS and first.isupper() and first.isalnum():
        out.append(re.compile(rf"(?<!\w){re.escape(first)}(?!\w)"))
    return out


@dataclass
class Company:
    label: str  # e.g. "_Companies/Salt"
    name: str  # e.g. "Salt"


class CompanyIndex:
    def __init__(self, labels: dict[str, int], company_folders: list[str]):
        """``labels`` maps every known label to its document count."""
        self.counts = labels
        self.companies: list[Company] = []
        patterns = [p.strip("/") for p in company_folders if p.strip("/")]
        seen = set()
        for label in labels:
            segs = label.split("/")
            for i in range(1, len(segs)):
                parent, label_i = "/".join(segs[:i]), "/".join(segs[: i + 1])
                if label_i in seen or segs[i] == "{year}":
                    continue
                if any(fnmatch.fnmatchcase(parent, p) for p in patterns):
                    seen.add(label_i)
                    self.companies.append(Company(label_i, segs[i]))
        self._patterns = {c.label: _text_patterns(c.name) for c in self.companies}

    def by_sender(self, sender: str) -> Company | None:
        hits = [c for c in self.companies if same_company(c.name, sender)]
        return min(hits, key=lambda c: len(c.label)) if hits else None

    def in_text(self, text: str) -> list[Company]:
        """Companies whose name appears near the top of the text, earliest (then longest) first."""
        head = (text or "")[:TEXT_WINDOW]
        found = []
        for c in self.companies:
            positions = [m.start() for p in self._patterns[c.label] if (m := p.search(head))]
            if positions:
                found.append((min(positions), -len(c.name), c))
        return [c for *_, c in sorted(found, key=lambda t: (t[0], t[1]))]

    @staticmethod
    def contains(company: Company, label: str) -> bool:
        return label == company.label or label.startswith(company.label + "/")

    def best_label(self, company: Company) -> str:
        """The label below the company that holds the most documents (the company folder itself
        if it has no subfolders with documents)."""
        inside = [(n, lbl) for lbl, n in self.counts.items() if self.contains(company, lbl)]
        if not inside:
            return company.label
        return max(inside, key=lambda t: (t[0], -len(t[1])))[1]


def folder_name(sender: str) -> str:
    return re.sub(r"\s+", " ", _BAD_SEGMENT.sub("-", sender)).strip(" .")


def apply_companies(
    preds: list[Prediction],
    index: CompanyIndex,
    *,
    text: str,
    sender: str | None,
    top_n: int,
    new_company_folder: str | None = None,
    is_excluded=lambda label: False,
) -> list[Prediction]:
    """Merge a company-based proposal into the rule/model proposals (``preds``, best first)."""
    rules = [p for p in preds if p.source == "rule"]
    model = [p for p in preds if p.source != "rule"]

    company = index.by_sender(sender) if sender else None
    if company is None and not sender:
        candidates = index.in_text(text)
        if candidates:
            # several names on the page (the postage stamp says "Post"): let the model decide
            mass = {c.label: sum(p.probability for p in model if index.contains(c, p.label)) for c in candidates}
            best = max(mass.values())
            company = next(c for c in candidates if mass[c.label] == best)

    extra: Prediction | None = None
    new: Prediction | None = None
    if company is not None:
        inside = [p for p in model if index.contains(company, p.label)]
        extra = inside[0] if inside else Prediction(index.best_label(company), 1.0, "sender")
        if is_excluded(extra.label):
            extra = None
    elif sender and new_company_folder and folder_name(sender):
        label = f"{new_company_folder}/{folder_name(sender)}"
        if not is_excluded(label):
            new = Prediction(label, 1.0, "new")

    out = list(rules)
    if extra is not None:
        rest = [p for p in model if p.label != extra.label]
        if rest and rest[0].probability >= CONFIDENT and (
            extra.source != "model" or rest[0].probability > extra.probability
        ):
            out.append(rest.pop(0))  # a confident model beats a company folder elsewhere
        out.append(extra)
        model = rest
    for p in model:
        if len(out) >= top_n - (1 if new else 0):
            break
        if all(p.label != q.label for q in out):
            out.append(p)
    out = out[: top_n - (1 if new else 0)]
    if new and all(p.label != new.label for p in out):
        out.append(new)
    return out[:top_n]
