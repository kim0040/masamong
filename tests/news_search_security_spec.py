import socket
import ssl
from urllib.parse import urlparse

import pytest

from utils import news_search


def _addr(ip: str):
    return (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))


def test_safe_url_rejects_any_non_global_dns_answer(monkeypatch):
    monkeypatch.setattr(
        news_search.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_addr("93.184.216.34"), _addr("127.0.0.1")],
    )

    assert news_search._is_safe_url("https://example.com/article") is False


def test_safe_url_accepts_public_https(monkeypatch):
    monkeypatch.setattr(
        news_search.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_addr("93.184.216.34")],
    )

    assert news_search._is_safe_url("https://example.com/article") is True


def test_resolver_preserves_os_address_order_and_caps_attempts(monkeypatch):
    ordered = [
        "2001:4860:4860::8888",
        "93.184.216.34",
        "2001:4860:4860::8844",
        "1.1.1.1",
        "8.8.8.8",
    ]
    monkeypatch.setattr(
        news_search.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_addr(address) for address in ordered],
    )

    _parsed, addresses, _port = news_search._resolve_public_addresses(
        "https://example.com/article"
    )

    assert addresses == ordered[:news_search._MAX_RESOLVED_ADDRESSES]


@pytest.mark.parametrize(
    "address",
    [
        "224.0.0.1",
        "ff0e::1",
        "fec0::1",
        "64:ff9b::7f00:1",
    ],
)
def test_safe_url_rejects_non_unicast_and_translation_addresses(
    monkeypatch,
    address,
):
    monkeypatch.setattr(
        news_search.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_addr(address)],
    )

    assert news_search._is_safe_url("https://example.com/article") is False


def test_redirect_target_is_revalidated(monkeypatch):
    checked_urls = []

    def fake_resolve(url):
        checked_urls.append(url)
        if url.endswith("/private"):
            raise ValueError("private target")
        return urlparse(url), ["93.184.216.34"], 443

    monkeypatch.setattr(news_search, "_resolve_public_addresses", fake_resolve)
    monkeypatch.setattr(
        news_search,
        "_request_pinned_once",
        lambda *args, **kwargs: news_search._PublicHTTPResponse(
            302,
            {"Location": "/private"},
            b"",
        ),
    )

    try:
        news_search._request_public_url("https://example.com/start", {})
    except ValueError:
        pass
    else:
        raise AssertionError("private redirect target must be rejected")

    assert checked_urls == [
        "https://example.com/start",
        "https://example.com/private",
    ]


def test_request_uses_the_already_validated_ip(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        news_search,
        "_resolve_public_addresses",
        lambda url: (urlparse(url), ["93.184.216.34"], 443),
    )

    def fake_pinned(url, headers, parsed, addresses, port, **kwargs):
        captured["addresses"] = list(addresses)
        captured["host"] = parsed.hostname
        return news_search._PublicHTTPResponse(
            200,
            {"Content-Type": "text/plain"},
            b"safe",
        )

    monkeypatch.setattr(news_search, "_request_pinned_once", fake_pinned)

    response = news_search._request_public_url("https://example.com/article", {})

    assert response.content == b"safe"
    assert captured == {
        "addresses": ["93.184.216.34"],
        "host": "example.com",
    }


def test_pinned_https_uses_validated_ip_and_original_tls_hostname(monkeypatch):
    captured = {}

    class FakeRawSocket:
        def settimeout(self, value):
            captured["raw_timeout"] = value

        def close(self):
            captured["raw_closed"] = True

    class FakeTLSSocket:
        def settimeout(self, value):
            captured["read_timeout"] = value

        def close(self):
            captured["tls_closed"] = True

    class FakeContext:
        def wrap_socket(self, raw_socket, *, server_hostname):
            captured["server_hostname"] = server_hostname
            return FakeTLSSocket()

    class FakeResponse:
        status = 200

        def getheaders(self):
            return [("content-length", "0")]

        def read(self, _size):
            return b""

        def close(self):
            captured["response_closed"] = True

    def fake_create_connection(target, timeout):
        captured["socket_target"] = target
        captured["connect_timeout"] = timeout
        return FakeRawSocket()

    def fake_request(connection, method, target, *, headers):
        captured["method"] = method
        captured["request_target"] = target
        captured["headers"] = dict(headers)
        connection.connect()

    monkeypatch.setattr(news_search.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(news_search.ssl, "create_default_context", FakeContext)
    monkeypatch.setattr(news_search.http.client.HTTPSConnection, "request", fake_request)
    monkeypatch.setattr(
        news_search.http.client.HTTPSConnection,
        "getresponse",
        lambda _connection: FakeResponse(),
    )

    response = news_search._request_pinned_once(
        "https://예시.한국/a;b?q=1",
        {},
        urlparse("https://예시.한국/a;b?q=1"),
        ["93.184.216.34"],
        443,
    )

    assert response.content == b""
    assert captured["socket_target"] == ("93.184.216.34", 443)
    assert captured["server_hostname"] == "xn--vv4b11d.xn--3e0b707e"
    assert captured["headers"]["Host"] == "xn--vv4b11d.xn--3e0b707e"
    assert captured["request_target"] == "/a;b?q=1"
    assert captured["response_closed"] is True
    assert captured["tls_closed"] is True


def test_tls_wrap_failure_closes_raw_socket(monkeypatch):
    captured = {"closed": False}

    class FakeRawSocket:
        def settimeout(self, _value):
            pass

        def close(self):
            captured["closed"] = True

    class FailingContext:
        def wrap_socket(self, _raw_socket, *, server_hostname):
            assert server_hostname == "example.com"
            raise ssl.SSLError("handshake failed")

    def fake_request(connection, _method, _target, *, headers):
        assert headers["Host"] == "example.com"
        connection.connect()

    monkeypatch.setattr(
        news_search.socket,
        "create_connection",
        lambda *args, **kwargs: FakeRawSocket(),
    )
    monkeypatch.setattr(news_search.ssl, "create_default_context", FailingContext)
    monkeypatch.setattr(news_search.http.client.HTTPSConnection, "request", fake_request)

    with pytest.raises(ValueError, match="공개 IP 연결에 실패"):
        news_search._request_pinned_once(
            "https://example.com/",
            {},
            urlparse("https://example.com/"),
            ["93.184.216.34"],
            443,
        )

    assert captured["closed"] is True


def test_response_charset_parsing_is_case_and_quote_tolerant():
    response = news_search._PublicHTTPResponse(
        200,
        {"Content-Type": 'text/html; Charset="EUC-KR"'},
        b"",
    )

    assert response.encoding == "EUC-KR"


def test_extract_rejects_content_type_substring_trick(monkeypatch):
    monkeypatch.setattr(
        news_search,
        "_request_public_url",
        lambda *args, **kwargs: news_search._PublicHTTPResponse(
            200,
            {"Content-Type": "application/not-text/html"},
            b"x" * 100,
        ),
    )

    text, reason = news_search._extract_article_text("https://example.com/article")

    assert text is None
    assert "Content-Type" in reason


def test_request_rejects_redirect_chain_past_limit(monkeypatch):
    requested_urls = []
    monkeypatch.setattr(
        news_search,
        "_resolve_public_addresses",
        lambda url: (urlparse(url), ["93.184.216.34"], 443),
    )

    def fake_request(url, *_args, **_kwargs):
        requested_urls.append(url)
        return news_search._PublicHTTPResponse(
            302,
            {"Location": f"/hop-{len(requested_urls)}"},
            b"",
        )

    monkeypatch.setattr(news_search, "_request_pinned_once", fake_request)

    with pytest.raises(ValueError, match="redirect 횟수"):
        news_search._request_public_url("https://example.com/start", {})

    assert len(requested_urls) == news_search._MAX_REDIRECTS + 1


def test_extract_rejects_streamed_body_past_size_limit(monkeypatch):
    monkeypatch.setattr(
        news_search,
        "_request_public_url",
        lambda *_args, **_kwargs: news_search._PublicHTTPResponse(
            200,
            {"Content-Type": "text/html"},
            b"x" * (news_search._MAX_ARTICLE_BYTES + 1),
        ),
    )

    text, reason = news_search._extract_article_text(
        "https://example.com/oversized"
    )

    assert text is None
    assert "2MB" in reason
