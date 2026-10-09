from html.parser import HTMLParser

import pytest

from print_center.app import create_app
from print_center.config import settings, source_code_url
from tests.test_api import FakeCups, FakeDiscovery


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


@pytest.mark.parametrize("value", ["", None, "https://github.com/example/family-print-center",
                                   "http://git.local:8080/print/tree/main", "https://例子.example/code",
                                   "http://[fd00::1]:8080/source", "https://example.org/source?download=1&version=2"])
def test_source_code_url_accepts_optional_absolute_http_links(value):
    assert source_code_url(value) == (value or "")


@pytest.mark.parametrize("value", ["javascript:alert(1)", "data:text/html,code", "file:///source", "//example.org/source",
                                   "/source", "https://", "https:///example.org/source", "http://[invalid]/source",
                                   "https://user:password@example.org/source", "https://user@example.org/source",
                                   "https://user%40example.org@other.example/source", "https://example.org:bad/source",
                                   "https://example.org:65536/source", "https://example.org\\other/source",
                                   "https://example.org/code\n", "https://exam\tple.org/code", "https://example.org/\x00",
                                   "https://bad_host.example/source", "https://example.org/" + "a" * 2048, 12, {}])
def test_source_code_url_refuses_unsafe_or_credential_links(value):
    with pytest.raises(ValueError, match="SOURCE_CODE_URL"):
        source_code_url(value)


def test_source_url_is_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("SOURCE_CODE_URL", "https://example.org/source")
    assert settings()["SOURCE_CODE_URL"] == "https://example.org/source"


@pytest.mark.parametrize("source", ["", "https://example.org/source?branch=main&download=1"])
def test_home_and_admin_offer_source_only_when_configured(tmp_path, source):
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "SECRET_KEY": "test-session-secret",
                      "SOURCE_CODE_URL": source}, cups_client=FakeCups(), discovery=FakeDiscovery())
    try:
        for page in ("/", "/admin"):
            response = app.test_client().get(page)
            assert response.status_code == 200
            parser = Links()
            parser.feed(response.get_data(as_text=True))
            matching = [link for link in parser.links if link.get("href") == source]
            assert len(matching) == int(bool(source))
            if source:
                assert matching[0]["target"] == "_blank"
                assert set(matching[0]["rel"].split()) == {"noopener", "noreferrer"}
                assert "原始碼</a>" in response.get_data(as_text=True)
    finally:
        app.extensions["uploads"].executor.shutdown(wait=True)


def test_invalid_source_configuration_fails_before_creating_application_data(tmp_path):
    data_dir = tmp_path / "not-created"
    with pytest.raises(ValueError, match="SOURCE_CODE_URL"):
        create_app({"TESTING": True, "DATA_DIR": str(data_dir), "SOURCE_CODE_URL": "javascript:alert(1)"})
    assert not data_dir.exists()
