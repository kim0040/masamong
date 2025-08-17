# -*- coding: utf-8 -*-
import asyncio
import ssl
import aiohttp
from aiohttp_retry import RetryClient, ExponentialRetry
from logger_config import logger

# 레거시 서버와의 SSL 핸드셰이크 문제를 해결하기 위한 SSL 컨텍스트
# 주의: 이 방법은 보안을 약화시킬 수 있으므로, 신뢰할 수 있는 특정 API에만 사용해야 합니다.
legacy_ssl_context = ssl.create_default_context()
legacy_ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
legacy_ssl_context.set_ciphers('DEFAULT@SECLEVEL=1')

async def get_aiohttp_session() -> aiohttp.ClientSession:
    """Aiohttp 클라이언트 세션을 반환하는 팩토리 함수."""
    # 필요에 따라 커넥터 등을 여기서 설정할 수 있습니다.
    return aiohttp.ClientSession()

async def make_async_request(url: str, method: str = "GET", params: dict = None, json_data: dict = None, headers: dict = None, use_legacy_ssl: bool = False) -> dict | None:
    """
    aiohttp와 aiohttp-retry를 사용하여 비동기 HTTP 요청을 수행하는 중앙 함수.

    :param url: 요청 URL
    :param method: HTTP 메서드 (GET, POST 등)
    :param params: URL 쿼리 파라미터
    :param json_data: POST 요청의 JSON 바디
    :param headers: 요청 헤더
    :param use_legacy_ssl: 레거시 SSL 컨텍스트 사용 여부
    :return: JSON 응답 또는 None
    """
    retry_options = ExponentialRetry(
        attempts=3,
        start_timeout=1,
        max_timeout=10,
        factor=2,
        statuses={500, 502, 503, 504} # 재시도할 HTTP 상태 코드
    )

    ssl_context = legacy_ssl_context if use_legacy_ssl else None

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            retry_client = RetryClient(client_session=session, retry_options=retry_options)

            async with retry_client.request(method, url, params=params, json=json_data, headers=headers, timeout=15) as response:
                response.raise_for_status()
                # 응답이 비어있는 경우를 대비
                if response.content_length == 0:
                    logger.warning(f"URL {url}에서 내용이 없는 빈 응답을 받았습니다.")
                    return None

                # 일부 API는 Content-Type을 application/json으로 보내지 않음
                content_type = response.headers.get('Content-Type', '')
                if 'json' in content_type or 'text' in content_type:
                    return await response.json(content_type=None)
                else:
                    logger.warning(f"URL {url}의 Content-Type이 JSON이나 텍스트가 아닙니다: {content_type}")
                    # 바이너리 데이터 등 다른 유형의 응답을 처리해야 할 경우 여기에 로직 추가
                    return None

    except aiohttp.ClientResponseError as e:
        logger.error(f"Aiohttp HTTP 오류: {e.status} for url: {url}. 메시지: {e.message}")
        return {"error": "http_error", "status": e.status, "message": f"API 서버 오류 ({e.status})"}
    except asyncio.TimeoutError:
        logger.error(f"Aiohttp 요청 시간 초과: {url}")
        return {"error": "timeout", "message": "API 요청 시간 초과"}
    except aiohttp.ClientError as e:
        logger.error(f"Aiohttp 클라이언트 오류: {e}", exc_info=True)
        return {"error": "client_error", "message": "API 요청 처리 중 오류 발생"}
    except Exception as e:
        logger.error(f"HTTP 요청 중 예기치 않은 오류 발생: {e}", exc_info=True)
        return {"error": "unknown_error", "message": "알 수 없는 오류 발생"}
