"""Tests for the save_fixture tool. A fake client is used; no network."""

import sys
from pathlib import Path

import pytest
import requests

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import save_fixture  # noqa: E402
from http_client import RobotsDisallowedError  # noqa: E402


def fake_response(
    status_code: int = 200,
    content: bytes = b"<html>tender page</html>",
) -> requests.Response:
    """Build a plain requests.Response without network access."""
    response = requests.Response()
    response.status_code = status_code
    response._content = content
    response.encoding = "utf-8"
    return response


class FakeClient:
    """Stands in for PoliteClient and records every requested URL."""

    def __init__(
        self,
        response: requests.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = (
            response if response is not None else fake_response()
        )
        self.error = error
        self.requested: list[str] = []

    def get(self, url: str, **kwargs: object) -> requests.Response:
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture()
def fixtures_dir(tmp_path, monkeypatch):
    """Redirect the fixtures directory into a tmp folder for the test."""
    monkeypatch.setattr(save_fixture, "FIXTURES_DIR", tmp_path)
    return tmp_path


class TestTargetPath:
    def test_appends_html_extension(self) -> None:
        assert save_fixture.target_path("listing").name == "listing.html"

    def test_does_not_double_html_extension(self) -> None:
        assert save_fixture.target_path("listing.html").name == "listing.html"

    def test_rejects_path_traversal(self) -> None:
        with pytest.raises(ValueError):
            save_fixture.target_path("../evil")

    def test_rejects_subdirectories(self) -> None:
        with pytest.raises(ValueError):
            save_fixture.target_path("sub/dir")


class TestSaveFixture:
    def test_saves_body_to_fixtures_dir(self, fixtures_dir) -> None:
        client = FakeClient(fake_response(content=b"<html>hello</html>"))
        path, response = save_fixture.save_fixture(
            "https://example.gov.pk/tenders/1", "listing", client=client
        )
        assert path == fixtures_dir / "listing.html"
        assert path.read_bytes() == b"<html>hello</html>"
        assert response.status_code == 200
        assert client.requested == ["https://example.gov.pk/tenders/1"]

    def test_refuses_to_overwrite_without_force(self, fixtures_dir) -> None:
        (fixtures_dir / "listing.html").write_bytes(b"old")
        with pytest.raises(FileExistsError):
            save_fixture.save_fixture(
                "https://example.gov.pk/t/1", "listing", client=FakeClient()
            )
        assert (fixtures_dir / "listing.html").read_bytes() == b"old"

    def test_force_overwrites(self, fixtures_dir) -> None:
        (fixtures_dir / "listing.html").write_bytes(b"old")
        path, _ = save_fixture.save_fixture(
            "https://example.gov.pk/t/1",
            "listing",
            force=True,
            client=FakeClient(),
        )
        assert path.read_bytes() == b"<html>tender page</html>"

    def test_refuses_when_robots_disallows(self, fixtures_dir) -> None:
        client = FakeClient(
            error=RobotsDisallowedError("robots.txt disallows fetching")
        )
        with pytest.raises(RobotsDisallowedError):
            save_fixture.save_fixture(
                "https://example.gov.pk/private/1", "listing", client=client
            )
        assert not (fixtures_dir / "listing.html").exists()

    def test_does_not_save_http_errors(self, fixtures_dir) -> None:
        client = FakeClient(fake_response(status_code=404))
        with pytest.raises(RuntimeError):
            save_fixture.save_fixture(
                "https://example.gov.pk/gone", "listing", client=client
            )
        assert not (fixtures_dir / "listing.html").exists()


class TestMain:
    URL = "https://example.gov.pk/tenders/1"

    def test_success_prints_status_size_and_preview(
        self, fixtures_dir, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CONTACT_EMAIL", "test@example.com")
        client = FakeClient(fake_response(content=b"<html>body</html>"))
        monkeypatch.setattr(save_fixture, "PoliteClient", lambda: client)
        exit_code = save_fixture.main([self.URL, "listing"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Status: 200" in out
        assert "Size:   17 bytes" in out
        assert "<html>body</html>" in out
        assert (fixtures_dir / "listing.html").read_bytes() == b"<html>body</html>"

    def test_robots_refusal_returns_exit_code_2(
        self, fixtures_dir, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CONTACT_EMAIL", "test@example.com")
        client = FakeClient(error=RobotsDisallowedError("robots.txt disallows"))
        monkeypatch.setattr(save_fixture, "PoliteClient", lambda: client)
        exit_code = save_fixture.main([self.URL, "listing"])
        assert exit_code == 2
        assert "robots.txt" in capsys.readouterr().err

    def test_warns_when_contact_email_missing(
        self, fixtures_dir, monkeypatch, capsys
    ) -> None:
        monkeypatch.delenv("CONTACT_EMAIL", raising=False)
        monkeypatch.setattr(
            save_fixture, "PoliteClient", lambda: FakeClient()
        )
        exit_code = save_fixture.main([self.URL, "listing"])
        assert exit_code == 0
        assert "CONTACT_EMAIL" in capsys.readouterr().err
