"""TF-IDF (word + char n-grams) + logistic regression classifier over folder labels."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import joblib

from scansort.config import Rule

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000


class NotEnoughData(Exception):
    pass


@dataclass
class Sample:
    text: str
    label: str
    sender: str | None = None


@dataclass
class ModelInfo:
    version: str
    trained_at: str
    samples: int
    labels: int
    cv_accuracy: float | None
    skipped_labels: list[str] = field(default_factory=list)


@dataclass
class Prediction:
    label: str
    probability: float
    source: str  # model | rule
    model_version: str | None = None


def _sender_token(sender: str | None) -> str:
    if not sender:
        return ""
    return " __sender_" + re.sub(r"\W+", "_", sender.lower()).strip("_")


def featurize(text: str, sender: str | None = None) -> str:
    return (text or "")[:MAX_TEXT_CHARS] + _sender_token(sender)


def build_pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, Pipeline

    return Pipeline(
        [
            (
                "features",
                FeatureUnion(
                    [
                        ("word", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, max_features=200_000)),
                        (
                            "char",
                            TfidfVectorizer(
                                analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True,
                                max_features=300_000,
                            ),
                        ),
                    ]
                ),
            ),
            ("clf", LogisticRegression(max_iter=3000, C=10.0, class_weight="balanced")),
        ]
    )


def train(samples: list[Sample], min_samples: int = 2):
    """Train a pipeline. Returns ``(pipeline, ModelInfo)``. Raises NotEnoughData."""
    samples = [s for s in samples if s.text and s.text.strip()]
    counts = Counter(s.label for s in samples)
    keep = {label for label, n in counts.items() if n >= min_samples}
    skipped = sorted(label for label in counts if label not in keep)
    samples = [s for s in samples if s.label in keep]
    if len(keep) < 2:
        raise NotEnoughData(
            f"need at least 2 folders with >= {min_samples} text documents each (have {len(keep)})"
        )
    X = [featurize(s.text, s.sender) for s in samples]
    y = [s.label for s in samples]

    cv_accuracy = None
    k = min(5, min(Counter(y).values()))
    if k >= 2:
        from sklearn.model_selection import StratifiedKFold, cross_val_score

        try:
            scores = cross_val_score(
                build_pipeline(), X, y, cv=StratifiedKFold(k, shuffle=True, random_state=0), n_jobs=1
            )
            cv_accuracy = float(scores.mean())
        except Exception:  # noqa: BLE001 - CV is informative only
            log.exception("cross-validation failed")

    pipeline = build_pipeline()
    pipeline.fit(X, y)
    now = datetime.now(UTC)
    info = ModelInfo(
        version=now.strftime("%Y%m%dT%H%M%SZ"),
        trained_at=now.isoformat(timespec="seconds"),
        samples=len(samples),
        labels=len(keep),
        cv_accuracy=cv_accuracy,
        skipped_labels=skipped,
    )
    return pipeline, info


class ModelStore:
    """Holds the current model; swapped atomically after training."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._pipeline = None
        self.info: ModelInfo | None = None

    @property
    def available(self) -> bool:
        return self._pipeline is not None

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            data = joblib.load(self.path)
            with self._lock:
                self._pipeline = data["pipeline"]
                self.info = ModelInfo(**data["info"])
            log.info("model loaded", extra={"version": self.info.version})
            return True
        except Exception:  # noqa: BLE001
            log.exception("could not load model; ignoring %s", self.path)
            return False

    def replace(self, pipeline, info: ModelInfo) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        joblib.dump({"pipeline": pipeline, "info": asdict(info)}, tmp)
        os.replace(tmp, self.path)
        with self._lock:
            self._pipeline = pipeline
            self.info = info

    def predict(self, text: str, sender: str | None = None, top_n: int = 3) -> list[Prediction]:
        with self._lock:
            pipeline, info = self._pipeline, self.info
        if pipeline is None or not (text or "").strip():
            return []
        probs = pipeline.predict_proba([featurize(text, sender)])[0]
        classes = pipeline.classes_
        ranked = sorted(zip(classes, probs, strict=True), key=lambda t: t[1], reverse=True)[:top_n]
        return [Prediction(str(lbl), float(p), "model", info.version if info else None) for lbl, p in ranked]


def propose(
    text: str,
    sender: str | None,
    *,
    store: ModelStore,
    rules: list[Rule],
    top_n: int,
    is_excluded=lambda label: False,
) -> list[Prediction]:
    out: list[Prediction] = []
    for rule in rules:
        if text and re.search(rule.pattern, text) and all(p.label != rule.label for p in out):
            out.append(Prediction(rule.label, 1.0, "rule"))
    for pred in store.predict(text, sender, top_n=top_n + len(out)):
        if len(out) >= top_n:
            break
        if is_excluded(pred.label) or any(p.label == pred.label for p in out):
            continue
        out.append(pred)
    return out[:top_n]
