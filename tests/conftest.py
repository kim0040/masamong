import os
import re
import socket
import sys
from pathlib import Path

import pytest
import pytest_asyncio  # noqa: F401
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 테스트 수집 시 config가 개발자의 실제 export/.env/config.json을 읽지 않도록 가장
# 먼저 격리한다. 명시 env는 파일 값을 우선하지만, 파일에 없는 운영 키가 상위
# 환경에 남아 있으면 테스트 프로세스로 유입될 수 있으므로 전체 키를 제거한다.
config_source = (ROOT / "config.py").read_text(encoding="utf-8")
config_env_keys = set(
    re.findall(
        r"(?:load_config_value|os\.environ\.get|os\.getenv)\(\s*['\"]([A-Z][A-Z0-9_]*)",
        config_source,
    )
)
example_env_keys = set(dotenv_values(ROOT / ".env.example"))
test_env_path = ROOT / "tests" / "fixtures" / "test.env"
test_values = {
    str(key): str(value or "")
    for key, value in dotenv_values(test_env_path, interpolate=False).items()
    if key
}
for key in config_env_keys | example_env_keys | set(test_values):
    os.environ.pop(key, None)

os.environ.update(test_values)
os.environ["MASAMONG_ENV_FILE"] = str(test_env_path)
os.environ["MASAMONG_CONFIG_FILE"] = str(ROOT / "tests" / "fixtures" / "config.test.json")
os.environ["MASAMONG_LOG_FILE"] = os.devnull
os.environ["MASAMONG_ERROR_LOG_FILE"] = os.devnull


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch):
    """단위 테스트가 실수로 운영 DB나 외부 API에 연결하는 것을 차단합니다."""

    def blocked(*args, **kwargs):
        raise RuntimeError("external network is disabled during tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
