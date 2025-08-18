# -*- coding: utf-8 -*-
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# A more modern and broadly compatible cipher suite string.
# This can help with servers that have specific TLS requirements.
CIPHERS = (
    'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:'
    'ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:'
    'DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384'
)

class ModernTlsAdapter(HTTPAdapter):
    """
    A custom HTTP adapter that forces a modern, specific set of TLS ciphers.
    """
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context(ciphers=CIPHERS)
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

def get_modern_tls_session() -> requests.Session:
    """
    Returns a requests.Session object configured with a modern TLS cipher suite.
    """
    session = requests.Session()
    session.mount('https://', ModernTlsAdapter())
    return session
