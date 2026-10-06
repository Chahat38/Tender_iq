"""Document pipeline: download, hash, store and extract tender documents.

A tender's PDF/DOCX attachments live on the source site until they
disappear (corrigenda, expired links). This module pulls them once,
stores them **content-addressed** (``sha256`` first-two-chars
subdirectory), records hash/size/mime on the ``opportunity_documents``
row and extracts plain text for PDFs and DOCX files so the search
index and the AI stage can read them without re-downloading.

Fetch policy:

* every request goes through :class:`~http_client.PoliteClient`, so
  robots.txt, the polite delay and the identifying User-Agent apply
  exactly as they do for listings;
* one attempt per URL. Failures (HTTP 4xx/5xx, robots refusal, oversized
  bodies) set ``fetched_at`` while leaving ``storage_path`` NULL -
  visible in the database but not auto-retried;
  :meth:`clear_failed_document_fetches` re-queues them on demand.

Files are written outside any web root, never executed, and identical
bytes from different tenders share one stored copy.
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from http_client import PoliteClient, RobotsDisallowedError, RobotsUnavailableError
from storage_common import utc_now_iso

logger = logging.getLogger(__name__)

#: Default on-disk root for stored documents (relative paths are stored
#: in the database so the root can move).
DOCUMENTS_DIR = Path("var") / "documents"

#: Refuse bodies larger than this (tender packs occasionally exceed
#: 50 MB; 25 MB keeps storage bounded while covering almost everything).
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

#: Cap for extracted text per document (SQLite/Postgres both handle far
#: more, but nobody needs page 4 000 of a scan).
EXTRACTED_TEXT_CAP = 200_000

_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"


def sha256_hex(data: bytes) -> str:
    """Hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def relative_storage_path(digest: str) -> str:
    """Content-addressed posix path (``ab/abcdef...``) for a digest."""
    return f"{digest[:2]}/{digest}"


def resolve_document_path(storage_path: str, documents_dir: Path) -> Path:
    """Turn a stored relative path back into a filesystem path."""
    return Path(documents_dir) / Path(storage_path)


def guess_mime(data: bytes, filename: str | None, header_type: str | None) -> str:
    """Best-effort content type: magic bytes win, then header, then name."""
    if data.startswith(_PDF_MAGIC):
        return "application/pdf"
    if data.startswith(_ZIP_MAGIC):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if header_type:
        return header_type.split(";", 1)[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def save_bytes(data: bytes, digest: str, documents_dir: Path) -> str:
    """Write ``data`` content-addressed under ``documents_dir``.

    Returns the relative storage path. Existing copies are left alone
    (identical bytes are identical bytes).
    """
    relative = relative_storage_path(digest)
    target = resolve_document_path(relative, documents_dir)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return relative


def extract_text(data: bytes, mime_type: str | None) -> str | None:
    """Extract plain text from PDF/DOCX bytes; None when not applicable.

    Returns the (possibly empty) text when extraction ran, None when the
    format is not extractable or the parser failed - the raw file stays
    stored either way. Output is capped at
    :data:`EXTRACTED_TEXT_CAP` characters.
    """
    if mime_type == "application/pdf" or data.startswith(_PDF_MAGIC):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            parts = [(page.extract_text() or "") for page in reader.pages]
            text = "\n".join(parts).strip()
        except Exception:  # noqa: BLE001 - a corrupt PDF must not abort the run
            logger.warning("PDF text extraction failed", exc_info=True)
            return None
        return text[:EXTRACTED_TEXT_CAP]

    if data.startswith(_ZIP_MAGIC):
        try:
            import docx

            document = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in document.paragraphs).strip()
        except Exception:  # noqa: BLE001 - not every zip is a docx
            logger.warning("DOCX text extraction failed", exc_info=True)
            return None
        return text[:EXTRACTED_TEXT_CAP]

    return None


def download_documents(
    storage: Any,
    *,
    documents_dir: Path | str = DOCUMENTS_DIR,
    client: Any = None,
    limit: int = 100,
    max_bytes: int = MAX_DOCUMENT_BYTES,
    retry_failed: bool = False,
) -> dict[str, int]:
    """Fetch every queued document; returns a per-outcome report.

    ``client`` defaults to a fresh :class:`PoliteClient` - tests inject
    a fake with a ``.get(url)`` method returning an object exposing
    ``status_code``, ``content`` and ``headers``.
    """
    documents_dir = Path(documents_dir)
    if retry_failed:
        requeued = storage.clear_failed_document_fetches()
        if requeued:
            logger.info("re-queued %d failed document fetches", requeued)
    if client is None:
        client = PoliteClient()

    report = {"downloaded": 0, "extracted": 0, "failed": 0}
    for doc in storage.list_documents_needing_fetch(limit):
        url = str(doc["url"])
        fetched_at = utc_now_iso()
        try:
            response = client.get(url)
        except (RobotsDisallowedError, RobotsUnavailableError):
            logger.warning("document refused by robots policy: %s", url)
            storage.mark_document_failed(doc["id"], fetched_at=fetched_at)
            report["failed"] += 1
            continue
        except Exception:  # noqa: BLE001 - timeouts etc.: one attempt only
            logger.warning("document fetch errored: %s", url, exc_info=True)
            storage.mark_document_failed(doc["id"], fetched_at=fetched_at)
            report["failed"] += 1
            continue

        if getattr(response, "status_code", 500) >= 400:
            logger.warning(
                "document fetch HTTP %s: %s",
                getattr(response, "status_code", "?"),
                url,
            )
            storage.mark_document_failed(doc["id"], fetched_at=fetched_at)
            report["failed"] += 1
            continue

        data: bytes = bytes(getattr(response, "content", b"") or b"")
        if not data:
            storage.mark_document_failed(doc["id"], fetched_at=fetched_at)
            report["failed"] += 1
            continue
        if len(data) > max_bytes:
            logger.warning(
                "document too large (%d bytes > %d): %s",
                len(data),
                max_bytes,
                url,
            )
            storage.mark_document_failed(doc["id"], fetched_at=fetched_at)
            report["failed"] += 1
            continue

        headers = getattr(response, "headers", {}) or {}
        mime_type = guess_mime(data, doc.get("filename"), headers.get("Content-Type"))
        digest = sha256_hex(data)
        storage.mark_document_fetched(
            doc["id"],
            content_hash=digest,
            storage_path=save_bytes(data, digest, documents_dir),
            mime_type=mime_type,
            byte_size=len(data),
            fetched_at=fetched_at,
        )
        report["downloaded"] += 1

        text = extract_text(data, mime_type)
        if text is not None:
            storage.mark_document_extracted(
                doc["id"], extracted_text=text, extracted_at=utc_now_iso()
            )
            report["extracted"] += 1
    return report


def source_of(url: str) -> str:
    """Origin of a document URL (used for logging/debugging)."""
    return urlparse(url).netloc
