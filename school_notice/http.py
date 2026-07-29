from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import aiohttp


class FetchError(RuntimeError):
    """안전한 공개 페이지 요청이 실패했을 때의 제한된 오류."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    byte_count: int
    elapsed_ms: int
    content_type: str


@dataclass(frozen=True)
class BinaryFetchResult:
    url: str
    final_url: str
    status: int
    data: bytes
    elapsed_ms: int
    content_type: str


class AsyncFetcher:
    _TLS12_ONLY_HOSTS = frozenset({"www.gachon.ac.kr"})

    def __init__(
        self,
        *,
        user_agent: str = (
            "Mozilla/5.0 (compatible; SchoolNoticeDigest/0.4; "
            "+public-academic-notices)"
        ),
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 3_000_000,
        max_binary_bytes: int = 20_000_000,
        max_requests: int = 30,
        max_retries: int = 2,
        min_host_interval_seconds: float = 0.2,
        min_request_interval_seconds: float = 0.0,
        respect_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_binary_bytes = max_binary_bytes
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.min_host_interval_seconds = max(0.0, float(min_host_interval_seconds))
        # 서로 다른 학교 호스트를 순회할 때도 요청이 연달아 몰리지 않게 하는
        # 전역 간격이다. 0이면 기존 동작을 유지한다.
        self.min_request_interval_seconds = max(
            0.0,
            float(min_request_interval_seconds),
        )
        self.respect_robots = respect_robots
        self.request_count = 0
        self.robots_notes: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._last_request_at: dict[str, float] = {}
        self._last_request_global_at: float | None = None
        self._pace_lock = asyncio.Lock()
        self._robots: dict[str, RobotFileParser | None] = {}
        self._validated_dns: set[str] = set()
        self._tls12_context = ssl.create_default_context()
        self._tls12_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls12_context.maximum_version = ssl.TLSVersion.TLSv1_2
        # 가천대 웹 서버는 OpenSSL의 기본 security level 2와 협상하지
        # 못한다. 이 완화는 해당 공개 호스트에만 적용하며 인증서 검증은
        # 그대로 유지한다.
        self._tls12_context.set_ciphers("DEFAULT:@SECLEVEL=1")

    def _ssl_for_host(self, host: str) -> ssl.SSLContext | bool:
        if host in self._TLS12_ONLY_HOSTS:
            return self._tls12_context
        return True

    async def __aenter__(self) -> "AsyncFetcher":
        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(8.0, self.timeout_seconds),
            sock_read=min(15.0, self.timeout_seconds),
        )
        connector = aiohttp.TCPConnector(
            limit=4,
            limit_per_host=1,
            ttl_dns_cache=300,
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            # 공개 게시판에는 사용자 프로필·Discord 식별자·쿠키·Referer를
            # 보내지 않는다. 요청 URL은 버전 관리된 source와 그 공개 목록에서
            # 추출한 공지 ID로만 구성된다.
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,*/*;q=0.8",
                "DNT": "1",
            },
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def _validate_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        allowed = {item.casefold() for item in allowed_hosts}
        if parsed.scheme not in {"http", "https"}:
            raise FetchError(f"unsupported_scheme:{parsed.scheme}")
        if not host or host not in allowed:
            raise FetchError(f"host_not_allowed:{host or 'missing'}")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal and (
            literal.is_private
            or literal.is_loopback
            or literal.is_link_local
            or literal.is_reserved
        ):
            raise FetchError("non_public_ip_literal")
        return host

    async def _pace(self, host: str) -> None:
        # 현재 수집기는 호출 자체가 순차적이지만, 재시도나 향후 호출자가
        # 실수로 동시 실행하더라도 간격 계산이 경합하지 않게 직렬화한다.
        async with self._pace_lock:
            now = time.monotonic()
            waits: list[float] = []
            previous = self._last_request_at.get(host)
            if previous is not None:
                waits.append(
                    self.min_host_interval_seconds - (now - previous)
                )
            if self._last_request_global_at is not None:
                waits.append(
                    self.min_request_interval_seconds
                    - (now - self._last_request_global_at)
                )
            remaining = max([0.0, *waits])
            if remaining > 0:
                await asyncio.sleep(remaining)
            requested_at = time.monotonic()
            self._last_request_at[host] = requested_at
            self._last_request_global_at = requested_at

    async def _validate_resolved_host(self, host: str) -> None:
        if host in self._validated_dns:
            return
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise FetchError(f"dns_resolution_failed:{host}") from exc
        addresses = {record[4][0] for record in records}
        if not addresses:
            raise FetchError(f"dns_resolution_empty:{host}")
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if (
                parsed.is_private
                or parsed.is_loopback
                or parsed.is_link_local
                or parsed.is_reserved
                or parsed.is_multicast
            ):
                raise FetchError(f"resolved_non_public_ip:{host}")
        self._validated_dns.add(host)

    def _reserve_request(self) -> None:
        if self.request_count >= self.max_requests:
            raise FetchError(f"request_budget_exhausted:{self.max_requests}")
        self.request_count += 1

    async def _read_bounded(
        self,
        response: aiohttp.ClientResponse,
        *,
        limit: int | None = None,
    ) -> bytes:
        size_limit = limit if limit is not None else self.max_response_bytes
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > size_limit:
            raise FetchError(
                f"response_too_large:{declared}>{size_limit}"
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > size_limit:
                raise FetchError(
                    f"response_too_large:{total}>{size_limit}"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode(data: bytes, content_type: str) -> str:
        charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
        candidates = [
            charset_match.group(1) if charset_match else None,
            "utf-8",
            "cp949",
            "euc-kr",
        ]
        decoded: list[tuple[int, str]] = []
        for charset in candidates:
            if not charset:
                continue
            try:
                text = data.decode(charset, errors="replace")
            except LookupError:
                continue
            decoded.append((text.count("\ufffd"), text))
        if not decoded:
            return data.decode("utf-8", errors="replace")
        return min(decoded, key=lambda item: item[0])[1]

    async def _robots_allowed(
        self,
        url: str,
        allowed_hosts: tuple[str, ...],
    ) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if host in self._robots:
            parser = self._robots[host]
            return parser is None or parser.can_fetch(self.user_agent, url)

        robots_url = f"{parsed.scheme}://{host}/robots.txt"
        try:
            await self._validate_resolved_host(host)
            self._reserve_request()
            await self._pace(host)
            assert self._session is not None
            async with self._session.get(
                robots_url,
                allow_redirects=True,
                ssl=self._ssl_for_host(host),
            ) as response:
                final_host = (response.url.host or "").casefold()
                if final_host not in {item.casefold() for item in allowed_hosts}:
                    raise FetchError(f"robots_redirect_host_not_allowed:{final_host}")
                if response.status in {401, 403}:
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
                    self._robots[host] = parser
                    self.robots_notes[host] = f"robots_http_{response.status}:deny"
                    return False
                if response.status == 200:
                    data = await self._read_bounded(response)
                    text = self._decode(data, response.headers.get("Content-Type", ""))
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(text.splitlines())
                    self._robots[host] = parser
                    self.robots_notes[host] = "robots_loaded"
                    return parser.can_fetch(self.user_agent, url)
                self._robots[host] = None
                self.robots_notes[host] = f"robots_http_{response.status}:allow"
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError, FetchError) as exc:
            self._robots[host] = None
            self.robots_notes[host] = f"robots_unavailable_allow:{type(exc).__name__}"
            return True

    async def fetch_text(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
    ) -> FetchResult:
        if self._session is None:
            raise RuntimeError("AsyncFetcher must be used as an async context manager")
        result = await self.fetch_bytes(
            url,
            allowed_hosts=allowed_hosts,
            max_bytes=self.max_response_bytes,
        )
        return FetchResult(
            url=result.url,
            final_url=result.final_url,
            status=result.status,
            text=self._decode(result.data, result.content_type),
            byte_count=len(result.data),
            elapsed_ms=result.elapsed_ms,
            content_type=result.content_type,
        )

    async def fetch_bytes(
        self,
        url: str,
        *,
        allowed_hosts: tuple[str, ...],
        max_bytes: int | None = None,
    ) -> BinaryFetchResult:
        if self._session is None:
            raise RuntimeError("AsyncFetcher must be used as an async context manager")
        host = self._validate_url(url, allowed_hosts)
        await self._validate_resolved_host(host)
        if not await self._robots_allowed(url, allowed_hosts):
            raise FetchError("robots_disallow")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._reserve_request()
                await self._pace(host)
                started = time.monotonic()
                async with self._session.get(
                    url,
                    allow_redirects=True,
                    ssl=self._ssl_for_host(host),
                ) as response:
                    final_url = str(response.url)
                    final_host = self._validate_url(final_url, allowed_hosts)
                    await self._validate_resolved_host(final_host)
                    if response.status == 429 or 500 <= response.status <= 599:
                        raise FetchError(f"retryable_http_status:{response.status}")
                    if response.status < 200 or response.status >= 300:
                        raise FetchError(f"http_status:{response.status}")
                    data = await self._read_bounded(
                        response,
                        limit=max_bytes or self.max_binary_bytes,
                    )
                    return BinaryFetchResult(
                        url=url,
                        final_url=final_url,
                        status=response.status,
                        data=data,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        content_type=response.headers.get("Content-Type", ""),
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, FetchError) as exc:
                last_error = exc
                retryable = isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))
                if isinstance(exc, FetchError):
                    retryable = str(exc).startswith("retryable_http_status:")
                if not retryable or attempt >= self.max_retries:
                    break
                await asyncio.sleep(0.4 * (2**attempt))
        raise FetchError(f"fetch_failed:{type(last_error).__name__}:{last_error}")
