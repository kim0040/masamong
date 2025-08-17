# -*- coding: utf-8 -*-
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# 일부 공공 API 서버의 오래된 TLS/SSL 설정과 호환성을 맞추기 위한 커스텀 설정.
# 보안 레벨을 낮춰 연결을 허용하므로, 신뢰할 수 있는 공공 API에만 제한적으로 사용해야 합니다.
CIPHERS = (
    'DEFAULT:@SECLEVEL=1'
)

class LegacySslAdapter(HTTPAdapter):
    """
    구형 TLS/SSL 프로토콜을 사용하는 서버에 연결하기 위한 커스텀 HTTP 어댑터.
    """
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context(ciphers=CIPHERS)
        kwargs['ssl_context'] = context
        return super(LegacySslAdapter, self).init_poolmanager(*args, **kwargs)

def get_legacy_ssl_session() -> requests.Session:
    """
    레거시 SSL/TLS 연결을 지원하는 requests.Session 객체를 반환합니다.

    경고: 이 세션은 일부 공공 API의 비표준 TLS 설정과의 호환성을 위해
    SSL 인증서 검증을 비활성화합니다 (`verify=False`).
    이는 중간자 공격(MITM)에 취약해질 수 있으므로, URL이 확실하게 신뢰할 수 있는
    공공 API 엔드포인트일 경우에만 제한적으로 사용해야 합니다.
    """
    session = requests.Session()
    session.mount('https://', LegacySslAdapter())
    session.verify = False  # 인증서 검증 비활성화
    return session
