import ssl

import pymysql
import pytest

from database.compat_db import TiDBSettings


def test_tidb_ca_enables_required_certificate_verification(tmp_path):
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("test-ca", encoding="utf-8")
    settings = TiDBSettings(
        host="db.example",
        port=4000,
        user="bot",
        password="secret",
        database="masamong",
        ssl_ca=str(ca_path),
        ssl_verify_identity=True,
        require_tls=True,
    )

    ssl_config = settings.to_connect_kwargs()["ssl"]

    assert ssl_config["ca"] == str(ca_path)
    assert ssl_config["check_hostname"] is True
    assert ssl_config["verify_mode"] == ssl.CERT_REQUIRED


def test_tidb_tls_options_build_a_verified_pymysql_context_without_network():
    # SSLContext 생성까지 실제 PyMySQL 경로를 검증할 수 있도록 시스템 CA를 쓴다.
    import certifi

    settings = TiDBSettings(
        host="db.example",
        port=4000,
        user="bot",
        password="secret",
        database="masamong",
        ssl_ca=certifi.where(),
        ssl_verify_identity=True,
        require_tls=True,
    )

    connection = pymysql.connect(
        **settings.to_connect_kwargs(),
        defer_connect=True,
    )

    assert connection.ssl is True
    assert connection.ctx.verify_mode == ssl.CERT_REQUIRED
    assert connection.ctx.check_hostname is True


def test_required_tls_without_ca_fails_closed():
    settings = TiDBSettings(
        host="db.example",
        port=4000,
        user="bot",
        password="secret",
        database="masamong",
        require_tls=True,
    )

    with pytest.raises(ValueError, match="CA"):
        settings.to_connect_kwargs()


def test_configured_missing_ca_fails_closed(tmp_path):
    settings = TiDBSettings(
        host="db.example",
        port=4000,
        user="bot",
        password="secret",
        database="masamong",
        ssl_ca=str(tmp_path / "missing-ca.pem"),
        require_tls=True,
    )

    with pytest.raises(ValueError, match="CA 파일"):
        settings.to_connect_kwargs()


def test_strict_remote_env_implies_tls_and_rejects_disabled_hostname_check(
    monkeypatch,
    tmp_path,
):
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("test-ca", encoding="utf-8")
    monkeypatch.setenv("MASAMONG_DB_STRICT_REMOTE_ONLY", "true")
    monkeypatch.delenv("MASAMONG_DB_REQUIRE_TLS", raising=False)
    monkeypatch.setenv("MASAMONG_DB_SSL_CA", str(ca_path))
    monkeypatch.setenv("MASAMONG_DB_SSL_VERIFY_IDENTITY", "true")
    monkeypatch.setenv("MASAMONG_DB_NAME", "masamong_general")

    settings = TiDBSettings.from_env()

    assert settings.require_tls is True
    assert settings.to_connect_kwargs()["ssl"]["check_hostname"] is True

    monkeypatch.setenv("MASAMONG_DB_SSL_VERIFY_IDENTITY", "false")
    with pytest.raises(ValueError, match="hostname"):
        TiDBSettings.from_env()


def test_embedding_store_does_not_replace_missing_configured_ca(
    monkeypatch,
    tmp_path,
):
    from utils import embeddings

    missing_ca = str(tmp_path / "configured-but-missing.pem")
    monkeypatch.setattr(embeddings.config, "TIDB_HOST", "db.example")
    monkeypatch.setattr(embeddings.config, "TIDB_PORT", 4000)
    monkeypatch.setattr(embeddings.config, "TIDB_USER", "bot")
    monkeypatch.setattr(embeddings.config, "TIDB_PASSWORD", "secret")
    monkeypatch.setattr(embeddings.config, "TIDB_NAME", "masamong")
    monkeypatch.setattr(embeddings.config, "TIDB_SSL_CA", missing_ca)
    monkeypatch.setattr(embeddings.config, "TIDB_SSL_VERIFY_IDENTITY", True)
    monkeypatch.setattr(embeddings.config, "REQUIRE_DB_TLS", True)

    settings = embeddings._build_tidb_settings()

    assert settings is not None
    assert settings.ssl_ca == missing_ca
    with pytest.raises(ValueError, match="CA 파일"):
        settings.to_connect_kwargs()


def test_tidb_env_connection_limits_reach_driver_kwargs(monkeypatch):
    monkeypatch.setenv("MASAMONG_DB_STRICT_REMOTE_ONLY", "false")
    monkeypatch.setenv("MASAMONG_DB_REQUIRE_TLS", "false")
    monkeypatch.setenv("MASAMONG_DB_SSL_CA", "")
    monkeypatch.setenv("MASAMONG_DB_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("MASAMONG_DB_READ_TIMEOUT", "19")
    monkeypatch.setenv("MASAMONG_DB_WRITE_TIMEOUT", "23")
    monkeypatch.setenv("MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS", "180")
    monkeypatch.setenv("MASAMONG_DB_NAME", "masamong_general")

    settings = TiDBSettings.from_env()
    kwargs = settings.to_connect_kwargs()

    assert settings.conn_max_lifetime_seconds == 180
    assert kwargs["connect_timeout"] == 7
    assert kwargs["read_timeout"] == 19
    assert kwargs["write_timeout"] == 23


def test_from_env_refuses_to_default_to_production_database(monkeypatch):
    # 예전 폴백은 미설정 시 운영 Masamo DB인 "masamong"을 대상으로 삼았다.
    # 프로필 없이 실행된 코드가 운영 데이터를 건드리지 않도록 명시를 요구한다.
    monkeypatch.delenv("MASAMONG_DB_NAME", raising=False)
    monkeypatch.setenv("MASAMONG_DB_STRICT_REMOTE_ONLY", "false")
    monkeypatch.setenv("MASAMONG_DB_REQUIRE_TLS", "false")
    monkeypatch.setenv("MASAMONG_DB_SSL_CA", "")

    with pytest.raises(ValueError, match="MASAMONG_DB_NAME"):
        TiDBSettings.from_env()

    monkeypatch.setenv("MASAMONG_DB_NAME", "   ")
    with pytest.raises(ValueError, match="MASAMONG_DB_NAME"):
        TiDBSettings.from_env()
