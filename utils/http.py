# -*- coding: utf-8 -*-
"""
HTTP 요청을 위한 커스텀 `requests.Session` 객체를 생성하는 유틸리티 모듈입니다.

다양한 서버의 TLS/SSL 요구사항에 대응하기 위해, 특정 TLS 버전이나
암호화 스위트를 강제하는 세션을 생성하는 함수들을 제공합니다.
"""

import requests
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# 최신 서버와의 호환성을 높이기 위한 암호화 스위트 목록
CIPHERS = (
    'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:'
    'ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:'
    'DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384'
)

class ModernTlsAdapter(HTTPAdapter):
    """최신 TLS 암호화 스위트를 강제하는 커스텀 HTTP 어댑터입니다."""
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context(ciphers=CIPHERS)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

class TlsV12Adapter(HTTPAdapter):
    """TLSv1.2 프로토콜을 강제하는 커스텀 HTTP 어댑터입니다.
    data.go.kr과 같은 구형 서버와의 호환성을 위해 사용됩니다.
    """
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

# --- 세션 생성 함수 --- #

def get_modern_tls_session() -> requests.Session:
    """최신 TLS 암호화 스위트를 사용하는 `requests.Session` 객체를 반환합니다."""
    session = requests.Session()
    session.mount('https://', ModernTlsAdapter())
    session.headers.update({
        'User-Agent': 'Masamong-Bot/5.2 (Discord Bot; +https://github.com/kim0040/masamong)'
    })
    return session

def get_tlsv12_session() -> requests.Session:
    """TLSv1.2를 강제하는 `requests.Session` 객체를 반환합니다."""
    session = requests.Session()
    session.mount('https://', TlsV12Adapter())
    session.headers.update({
        'User-Agent': 'Masamong-Bot/5.2 (Discord Bot; +https://github.com/kim0040/masamong)'
    })
    return session

def get_insecure_session() -> requests.Session:
    """
    [주의] SSL 인증서 검증을 비활성화하는 `requests.Session` 객체를 반환합니다.
    알려진 인증서 문제가 있는 특정 API에 대해서만 매우 신중하게 사용해야 합니다.
    """
    session = requests.Session()
    session.verify = False
    session.headers.update({
        'User-Agent': 'Masamong-Bot/5.2 (Discord Bot; +https://github.com/kim0040/masamong)'
    })
    return session
