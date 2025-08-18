# -*- coding: utf-8 -*-
import requests

# Suppress only the single InsecureRequestWarning from urllib3 needed for self-signed certs.
requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

def get_http_session() -> requests.Session:
    """
    Returns a requests.Session object with SSL verification disabled.
    This is a workaround for environments with certificate trust issues (e.g., Windows)
    and APIs that use self-signed or problematic certificates.
    """
    session = requests.Session()
    session.verify = False
    return session
