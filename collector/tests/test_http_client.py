"""Tests for PoliteClient. robots.txt is mocked; no test touches the network."""

import pytest
import requests
from http_client import PoliteClient, RobotsDisallowedError, RobotsUnavailableError

BASE = "https://tenders.example.pk"
ROBOTS_DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private/\n"


class RecordingSleeper:
    """Stands in for time.sleep and records every requested delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(robots_text: str | None, sleeper: RecordingSleeper | None = None, **kwargs):
    """Build a PoliteClient with a mocked robots.txt and no real sleeping."""
    sleeper = sleeper or RecordingSleeper()
    client = PoliteClient(
        delay=0,
        robots_fetcher=lambda robots_url: robots_text,
        sleep_func=sleeper,
        contact_email="test@example.com",
        **kwargs,
    )
    return client, sleeper


def fake_response(status_code: int = 200, text: str = "ok") -> requests.Response:
    """Build a plain requests.Response without any network access."""
    response = requests.Response()
    response.status_code = status_code
    response._content = text.encode("utf-8")
    return response


class TestRobotsChecking:
    def test_refuses_disallowed_url_without_requesting_it(self) -> None:
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE)

        def must_not_request(*args, **kwargs):
            raise AssertionError("must not send a request for a disallowed URL")

        client.session.get = must_not_request
        with pytest.raises(RobotsDisallowedError) as excinfo:
            client.get(f"{BASE}/private/listing/1")
        assert "robots.txt" in str(excinfo.value)
        assert "/private/listing/1" in str(excinfo.value)

    def test_allows_permitted_url(self) -> None:
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE)
        client.session.get = lambda *a, **k: fake_response(200, "<html>listing</html>")
        response = client.get(f"{BASE}/open/listing/1")
        assert response.status_code == 200
        assert response.text == "<html>listing</html>"

    def test_refuses_when_robots_unreachable(self) -> None:
        client, _ = make_client(None)  # robots.txt could not be fetched
        with pytest.raises(RobotsUnavailableError):
            client.get(f"{BASE}/anything")

    def test_disallow_all_rules_block_everything(self) -> None:
        client, _ = make_client("User-agent: *\nDisallow: /\n")
        with pytest.raises(RobotsDisallowedError):
            client.get(f"{BASE}/listing/1")

    def test_user_agent_contains_bot_name_and_contact(self) -> None:
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE)
        agent = client.session.headers["User-Agent"]
        assert agent.startswith("TenderScoutBot/0.1")
        assert "test@example.com" in agent


class TestRetries:
    def test_retries_on_5xx_with_backoff_then_succeeds(self) -> None:
        sleeper = RecordingSleeper()
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE, sleeper=sleeper)
        responses = [fake_response(500), fake_response(500), fake_response(200)]
        client.session.get = lambda *a, **k: responses.pop(0)
        assert client.get(f"{BASE}/open/listing").status_code == 200
        assert sleeper.calls == [1.0, 2.0]  # exponential backoff: 1s, 2s

    def test_gives_up_after_max_attempts(self) -> None:
        sleeper = RecordingSleeper()
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE, sleeper=sleeper, max_attempts=3)
        client.session.get = lambda *a, **k: fake_response(503)
        with pytest.raises(requests.HTTPError):
            client.get(f"{BASE}/open/listing")
        assert len(sleeper.calls) == 2

    def test_does_not_retry_client_errors(self) -> None:
        sleeper = RecordingSleeper()
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE, sleeper=sleeper)
        client.session.get = lambda *a, **k: fake_response(404)
        response = client.get(f"{BASE}/open/gone")
        assert response.status_code == 404
        assert sleeper.calls == []

    def test_retries_on_429(self) -> None:
        sleeper = RecordingSleeper()
        client, _ = make_client(ROBOTS_DISALLOW_PRIVATE, sleeper=sleeper)
        responses = [fake_response(429), fake_response(200)]
        client.session.get = lambda *a, **k: responses.pop(0)
        assert client.get(f"{BASE}/open/listing").status_code == 200
        assert len(sleeper.calls) == 1
