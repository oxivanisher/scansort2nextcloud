"""PDF text extraction (text layer via pypdf) and OCR (ocrmypdf subprocess)."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

log = logging.getLogger(__name__)


class OcrError(Exception):
    pass


@dataclass
class PdfText:
    text: str
    page_count: int


def read_pdf(source: Path | bytes) -> PdfText:
    """Extract the text layer. Raises on unreadable PDFs."""
    stream = io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")  # noqa: SIM115
    try:
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise ValueError("PDF is encrypted") from exc
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - broken page content streams
                parts.append("")
        return PdfText(text="\n".join(parts).strip(), page_count=len(reader.pages))
    finally:
        stream.close()


def meaningful_chars(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


def ocr_available() -> bool:
    return shutil.which("ocrmypdf") is not None


def run_ocr(src: Path, dst: Path, languages: list[str], timeout: int = 900, extra_args: list[str] = ()) -> None:
    exe = shutil.which("ocrmypdf")
    if exe is None:
        raise OcrError("ocrmypdf is not installed")
    cmd = [
        exe,
        "--skip-text",
        "--output-type", "pdf",
        "--rotate-pages",
        "-l", "+".join(languages),
        *extra_args,
        str(src),
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"OCR timed out after {timeout}s") from exc
    # exit code 6 = "already has text" which --skip-text should avoid; treat as success if output exists
    if proc.returncode not in (0, 6) or not dst.exists():
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise OcrError(f"ocrmypdf exit {proc.returncode}: {' | '.join(tail)}")
