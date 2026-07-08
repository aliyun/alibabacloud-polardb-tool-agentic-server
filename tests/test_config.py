import os
import warnings
from unittest.mock import patch


from server.config import AppConfig, LoggingConfig, load_config, reset_config, get_config, SQLSecurityConfig


class TestConfigDefaults:
    def test_default_server_config(self):
        config = AppConfig(server={"dev_mode": True})
        assert config.server.port == 18760
        assert config.server.host == "0.0.0.0"
        assert config.server.log_level == "info"

    def test_default_auth_config(self):
        config = AppConfig(server={"dev_mode": True})
        assert config.auth.mode == "builtin"
        assert config.auth.jwt.algorithm == "RS256"

    def test_default_database_config(self):
        config = AppConfig(server={"dev_mode": True})
        assert "sqlite" in config.database.url

    def test_default_sql_security(self):
        config = AppConfig(server={"dev_mode": True})
        assert config.sql_security.max_rows == 1000
        assert config.sql_security.rate_limit.requests_per_minute == 60

    def test_default_public_base_url(self):
        with patch.dict(os.environ, {"PAS_SERVER_DEV_MODE": "true"}):
            config = load_config("/nonexistent")
        assert config.server.public_base_url == "http://localhost:18760"

    def test_public_base_url_localhost_rejected_in_production(self):
        """Fail-fast: production mode rejects localhost public_base_url."""
        import pytest
        with pytest.raises(Exception, match="localhost"):
            AppConfig(server={"dev_mode": False, "public_base_url": "http://localhost:18760"})

    def test_public_base_url_production_ok(self):
        config = AppConfig(server={"dev_mode": False, "public_base_url": "https://mcp.example.com"})
        assert config.server.public_base_url == "https://mcp.example.com"

    def test_oidc_manual_endpoints(self):
        with patch.dict(os.environ, {"PAS_SERVER_DEV_MODE": "true"}):
            config = load_config("/nonexistent")
        assert config.auth.oidc.authorization_endpoint is None
        assert config.auth.oidc.token_endpoint is None
        assert config.auth.oidc.userinfo_endpoint is None
        assert config.auth.oidc.jwks_uri is None
        assert config.auth.oidc.userinfo_token_method == "bearer_header"

    def test_oidc_new_configurable_fields(self):
        with patch.dict(os.environ, {"PAS_SERVER_DEV_MODE": "true"}):
            config = load_config("/nonexistent")
        assert config.auth.oidc.provider_name == "oidc"
        assert config.auth.oidc.idp_pkce is False
        assert config.auth.oidc.id_token_algorithms == ["RS256", "ES256"]


class TestConfigLoading:
    def test_load_config_no_file(self, tmp_path):
        with patch.dict(os.environ, {"PAS_SERVER_DEV_MODE": "true"}):
            config = load_config(tmp_path / "nonexistent.yaml")
        assert config.server.port == 18760

    def test_load_config_from_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("server:\n  port: 9999\n  log_level: debug\n  dev_mode: true\n")
        config = load_config(config_file)
        assert config.server.port == 9999
        assert config.server.log_level == "debug"
        assert config.server.host == "0.0.0.0"  # default preserved

    def test_env_override_takes_precedence(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("server:\n  port: 9999\n  dev_mode: true\n")
        with patch.dict(os.environ, {"PAS_SERVER_PORT": "7777"}):
            config = load_config(config_file)
        assert config.server.port == 7777

    def test_env_override_database_url(self):
        with patch.dict(os.environ, {"PAS_DATABASE_URL": "mysql+asyncmy://localhost/test", "PAS_SERVER_DEV_MODE": "true"}):
            config = load_config("/nonexistent/path.yaml")
        assert config.database.url == "mysql+asyncmy://localhost/test"

    def test_env_override_oidc_fields(self):
        env = {
            "PAS_SERVER_DEV_MODE": "true",
            "PAS_OIDC_PROVIDER_NAME": "generic-oidc",
            "PAS_OIDC_IDP_PKCE": "true",
            "PAS_OIDC_SCOPES": "openid,profile",
            "PAS_OIDC_ID_TOKEN_ALGORITHMS": "RS256,ES384",
            "PAS_OIDC_AUTHORIZATION_ENDPOINT": "https://login.example.com/authorize",
            "PAS_OIDC_TOKEN_ENDPOINT": "https://login.example.com/token",
            "PAS_OIDC_USERINFO_ENDPOINT": "https://login.example.com/userinfo",
            "PAS_OIDC_USERINFO_TOKEN_METHOD": "form_post",
        }
        with patch.dict(os.environ, env):
            config = load_config("/nonexistent/path.yaml")
        assert config.auth.oidc.provider_name == "generic-oidc"
        assert config.auth.oidc.idp_pkce is True
        assert config.auth.oidc.scopes == ["openid", "profile"]
        assert config.auth.oidc.id_token_algorithms == ["RS256", "ES384"]
        assert config.auth.oidc.authorization_endpoint == "https://login.example.com/authorize"
        assert config.auth.oidc.token_endpoint == "https://login.example.com/token"
        assert config.auth.oidc.userinfo_endpoint == "https://login.example.com/userinfo"
        assert config.auth.oidc.userinfo_token_method == "form_post"


class TestConfigSingleton:
    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_get_config_returns_same_instance(self):
        with patch.dict(os.environ, {"PAS_SERVER_DEV_MODE": "true"}):
            c1 = get_config()
            c2 = get_config()
        assert c1 is c2


class TestLoggingConfig:
    def test_defaults(self):
        config = LoggingConfig()
        assert config.log_dir == "log"
        assert config.log_file == "alibabacloud-polardb-tool-agentic-server.log"
        assert config.max_bytes == 100 * 1024 * 1024
        assert config.backup_count == 10
        assert config.timezone == "UTC+8"

    def test_appconfig_logging_defaults(self):
        config = AppConfig(server={"dev_mode": True})
        assert config.logging.log_dir == "log"
        assert config.logging.timezone == "UTC+8"

    def test_env_override_logging(self):
        env = {
            "PAS_SERVER_DEV_MODE": "true",
            "PAS_LOGGING_LOG_DIR": "/var/log/app",
            "PAS_LOGGING_LOG_FILE": "custom.log",
            "PAS_LOGGING_MAX_BYTES": "5242880",
            "PAS_LOGGING_BACKUP_COUNT": "5",
            "PAS_LOGGING_TIMEZONE": "UTC",
        }
        with patch.dict(os.environ, env):
            config = load_config("/nonexistent/path.yaml")
        assert config.logging.log_dir == "/var/log/app"
        assert config.logging.log_file == "custom.log"
        assert config.logging.max_bytes == 5242880
        assert config.logging.backup_count == 5
        assert config.logging.timezone == "UTC"


class TestConnectionPoolConfig:
    def test_defaults(self):
        from server.config import ConnectionPoolConfig
        cfg = ConnectionPoolConfig()
        assert cfg.idle_timeout_seconds == 1800
        assert cfg.max_total_pools == 200
        assert cfg.health_check is True
        assert cfg.cleanup_interval_s == 60

    def test_override(self):
        from server.config import ConnectionPoolConfig
        cfg = ConnectionPoolConfig(idle_timeout_seconds=600, health_check=False, cleanup_interval_s=30)
        assert cfg.idle_timeout_seconds == 600
        assert cfg.health_check is False
        assert cfg.cleanup_interval_s == 30


class TestSQLSecurityConfigMigration:
    def test_new_defaults(self):
        config = SQLSecurityConfig()
        assert config.confirmable_statement_types == ["DROP", "TRUNCATE", "ALTER", "DELETE"]
        assert config.blocked_keywords == ["DROP DATABASE"]
        assert config.blocked_statement_types is None

    def test_old_config_migrated(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = SQLSecurityConfig(blocked_statement_types=["DROP", "ALTER"])
        assert config.confirmable_statement_types == ["DROP", "ALTER"]
        assert any("deprecated" in str(x.message).lower() for x in w)

    def test_both_present_new_wins(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = SQLSecurityConfig(
                blocked_statement_types=["DROP"],
                confirmable_statement_types=["DROP", "TRUNCATE", "ALTER", "DELETE"],
            )
        assert config.confirmable_statement_types == ["DROP", "TRUNCATE", "ALTER", "DELETE"]
        assert any("deprecated" in str(x.message).lower() for x in w)
