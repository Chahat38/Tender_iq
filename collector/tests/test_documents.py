"""Document pipeline tests (offline; HTTP is faked)."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import documents
import docx
import pytest
from http_client import RobotsDisallowedError
from models import NormalizedOpportunity
from storage import Storage

DEADLINE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF pypdf can actually extract ``text`` from."""
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


def minimal_docx(text: str) -> bytes:
    """A one-paragraph DOCX via python-docx."""
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class FakeClient:
    """Maps URL -> response or exception; records requested URLs."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "docs.db")
    try:
        yield st
    finally:
        st.close()


def add_doc(storage, url: str, *, title: str = "Tender") -> int:
    source_id = storage.get_or_create_source("Doc Source", "DocAdapter")
    opp = NormalizedOpportunity(
        external_id=url.rsplit("/", 1)[-1],
        source_url="https://example.org/tender",
        source_name="Doc Source",
        title=title,
        deadline=DEADLINE,
        document_urls=[url],
    )
    storage.upsert_opportunity(opp, source_id)
    row = storage.conn.execute(
        "SELECT id FROM opportunity_documents WHERE url = ?", (url,)
    ).fetchone()
    return int(row["id"])


class TestHelpers:
    def test_sha256_and_relative_path(self) -> None:
        digest = documents.sha256_hex(b"abc")
        assert len(digest) == 64
        assert documents.relative_storage_path(digest) == f"{digest[:2]}/{digest}"

    def test_save_bytes_is_content_addressed(self, tmp_path) -> None:
        data = b"same bytes"
        digest = documents.sha256_hex(data)
        first = documents.save_bytes(data, digest, tmp_path)
        target = tmp_path / first
        target.write_bytes(b"corrupt me")  # second write must not clobber
        documents.save_bytes(data, digest, tmp_path)
        assert target.read_bytes() == b"corrupt me"
        assert first == documents.save_bytes(data, digest, tmp_path / "elsewhere")

    def test_guess_mime_prefers_magic_over_header(self) -> None:
        assert (
            documents.guess_mime(b"%PDF-1.4", "x.pdf", "text/html")
            == "application/pdf"
        )
        assert (
            documents.guess_mime(b"PK\x03\x04", "x", None)
            == "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )
        assert documents.guess_mime(b"hi", "a.txt", None) == "text/plain"
        assert (
            documents.guess_mime(b"hi", "a.txt", "text/plain; charset=utf-8")
            == "text/plain"
        )
        assert documents.guess_mime(b"??", None, None) == "application/octet-stream"

    def test_extract_text_pdf_docx_and_plain(self) -> None:
        pdf = minimal_pdf("Hello Tender")
        assert "Hello Tender" in (documents.extract_text(pdf, "application/pdf") or "")
        docx_bytes = minimal_docx("Scope of work")
        assert "Scope of work" in (
            documents.extract_text(docx_bytes, None) or ""
        )
        assert documents.extract_text(b"just text", "text/plain") is None
        assert documents.extract_text(b"%PDF-corrupt", "application/pdf") is None


class TestDownload:
    def test_successful_pdf_download_and_extraction(self, tmp_path, storage) -> None:
        url = "https://example.org/docs/tor.pdf"
        doc_id = add_doc(storage, url)
        pdf = minimal_pdf("Terms of Reference")
        client = FakeClient({url: FakeResponse(pdf, headers={"Content-Type": "application/pdf"})})

        report = documents.download_documents(
            storage, documents_dir=tmp_path / "store", client=client
        )
        assert report == {"downloaded": 1, "extracted": 1, "failed": 0}

        row = storage.get_documents(1)[0]
        digest = documents.sha256_hex(pdf)
        assert row["id"] == doc_id
        assert row["content_hash"] == digest
        assert row["byte_size"] == len(pdf)
        assert row["mime_type"] == "application/pdf"
        assert row["storage_path"] == documents.relative_storage_path(digest)
        assert row["fetched_at"]
        assert "Terms of Reference" in row["extracted_text"]
        assert row["extracted_at"]
        stored = documents.resolve_document_path(row["storage_path"], tmp_path / "store")
        assert stored.read_bytes() == pdf
        assert storage.list_documents_needing_fetch() == []

    def test_identical_bytes_share_one_stored_copy(self, tmp_path, storage) -> None:
        url_a, url_b = "https://example.org/a.pdf", "https://example.org/b.pdf"
        add_doc(storage, url_a, title="A")
        add_doc(storage, url_b, title="B")
        pdf = minimal_pdf("Shared pack")
        client = FakeClient(
            {url_a: FakeResponse(pdf), url_b: FakeResponse(pdf)}
        )
        report = documents.download_documents(
            storage, documents_dir=tmp_path / "store", client=client
        )
        assert report["downloaded"] == 2
        rows = storage.conn.execute(
            "SELECT storage_path, COUNT(*) AS n FROM opportunity_documents "
            "GROUP BY storage_path"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["n"] == 2

    def test_http_error_marks_attempted_not_stored(self, storage, tmp_path) -> None:
        url = "https://example.org/gone.pdf"
        add_doc(storage, url)
        client = FakeClient({url: FakeResponse(b"nope", status_code=404)})
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client
        )
        assert report == {"downloaded": 0, "extracted": 0, "failed": 1}
        row = storage.get_documents(1)[0]
        assert row["fetched_at"]  # attempted...
        assert row["storage_path"] is None  # ...but nothing stored
        assert storage.list_documents_needing_fetch() == []  # no auto-retry

        # Explicit retry re-queues and succeeds this time.
        assert storage.clear_failed_document_fetches() == 1
        client = FakeClient({url: FakeResponse(minimal_pdf("Back"))})
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client, retry_failed=True
        )
        assert report["downloaded"] == 1

    def test_robots_refusal_counts_as_failed(self, storage, tmp_path) -> None:
        url = "https://blocked.example.org/x.pdf"
        add_doc(storage, url)
        client = FakeClient({url: RobotsDisallowedError("nope")})
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client
        )
        assert report["failed"] == 1
        row = storage.get_documents(1)[0]
        assert row["fetched_at"] and row["storage_path"] is None

    def test_oversized_body_rejected(self, storage, tmp_path) -> None:
        url = "https://example.org/huge.pdf"
        add_doc(storage, url)
        client = FakeClient({url: FakeResponse(b"x" * 100)})
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client, max_bytes=50
        )
        assert report["failed"] == 1
        assert storage.get_documents(1)[0]["storage_path"] is None

    def test_plain_text_stored_but_not_extracted(self, storage, tmp_path) -> None:
        url = "https://example.org/notice.txt"
        add_doc(storage, url)
        client = FakeClient(
            {url: FakeResponse(b"notice body", headers={"Content-Type": "text/plain"})}
        )
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client
        )
        assert report == {"downloaded": 1, "extracted": 0, "failed": 0}
        row = storage.get_documents(1)[0]
        assert row["storage_path"]
        assert row["extracted_text"] is None
        assert row["extracted_at"] is None

    def test_limit_is_respected(self, storage, tmp_path) -> None:
        urls = [f"https://example.org/{n}.txt" for n in range(3)]
        for url in urls:
            add_doc(storage, url, title=url)
        client = FakeClient({url: FakeResponse(b"x") for url in urls})
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client, limit=2
        )
        assert report["downloaded"] == 2
        assert len(storage.list_documents_needing_fetch()) == 1

    def test_empty_body_counts_as_failed(self, storage, tmp_path) -> None:
        url = "https://example.org/empty.pdf"
        add_doc(storage, url)
        client = FakeClient({url: FakeResponse(b"")})
        report = documents.download_documents(
            storage, documents_dir=tmp_path, client=client
        )
        assert report["failed"] == 1


class TestCli:
    def test_documents_flag_prints_report(self, tmp_path, capsys, monkeypatch) -> None:
        import main as main_module

        captured = {}

        def fake_download(storage, **kwargs):
            captured.update(kwargs)
            return {"downloaded": 2, "extracted": 1, "failed": 0}

        monkeypatch.setattr(main_module, "download_documents", fake_download)
        code = main_module.main(
            ["--documents", "--db", str(tmp_path / "cli.db"),
             "--docs-limit", "7", "--docs-dir", str(tmp_path / "d")]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("Documents: downloaded 2, extracted 1, failed 0")
        assert captured["limit"] == 7
        assert captured["documents_dir"] == str(tmp_path / "d")

    def test_documents_rejects_combination_with_dedup_only(self, tmp_path) -> None:
        import main as main_module

        with pytest.raises(SystemExit):
            main_module.parse_args(["--documents", "--dedup-only"])
