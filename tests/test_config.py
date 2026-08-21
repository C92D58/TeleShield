"""config.py 存儲測試（隔離目錄）。"""

import os

import pytest

import teleshield.config as config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """每個測試使用獨立 TELESHIELD_HOME。"""
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "user.session")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "BLOCK_LOG", tmp_path / "block_log.json")
    monkeypatch.setattr(config, "LEARNED_FILE", tmp_path / "learned_patterns.json")
    return tmp_path


class TestConfig:
    def test_load_empty(self, isolated_home):
        assert config.load_config() == {}

    def test_save_load_roundtrip(self, isolated_home):
        cfg = {"api_id": 123, "name": "測試"}
        config.save_config(cfg)
        assert config.load_config() == cfg

    def test_file_permission_600(self, isolated_home):
        config.save_config({"api_id": 1})
        mode = os.stat(isolated_home / "config.json").st_mode & 0o777
        assert mode == 0o600, f"config.json 權限應為 600，實際 {oct(mode)}"

    def test_corrupt_config_returns_empty(self, isolated_home):
        (isolated_home / "config.json").write_text("{broken json")
        assert config.load_config() == {}

    def test_secure_session_file_600(self, isolated_home):
        # Telethon session 文件默認 644——secure_session_file 應收緊為 600
        session = isolated_home / "user.session"
        session.write_bytes(b"fake telethon session data")
        session.chmod(0o644)
        config.secure_session_file()
        assert session.stat().st_mode & 0o777 == 0o600

    def test_secure_session_file_missing(self, isolated_home):
        # session 不存在時不應報錯
        config.secure_session_file()


class TestBlockLog:
    def test_load_empty(self, isolated_home):
        assert config.load_block_log() == {"blocks": []}

    def test_log_block_caps_500(self, isolated_home):
        for i in range(510):
            config.log_block(i, f"user{i}", "spam", "private")
        log = config.load_block_log()
        assert len(log["blocks"]) == 500
        assert log["blocks"][0]["user_id"] == 10  # 最舊 10 筆被丟棄

    def test_log_block_fields(self, isolated_home):
        config.log_block(123, "小明", "加我微信", "private")
        block = config.load_block_log()["blocks"][0]
        assert block["user_id"] == 123
        assert block["source"] == "private"
        assert block["time"]  # ISO 時間存在


class TestLists:
    def test_blacklist_check(self):
        cfg = {"blacklist": {"999": {"reason": "manual"}}}
        assert config.is_blacklisted(999, cfg) is True
        assert config.is_blacklisted(100, cfg) is False

    def test_whitelist_check(self):
        cfg = {"whitelist": {"1": {}}}
        assert config.is_whitelisted(1, cfg) is True
        assert config.is_whitelisted(2, cfg) is False


class TestDotenv:
    def test_load_dotenv(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("# 註釋\nTELESHIELD_API_ID=12345\nTELESHIELD_API_HASH=abc123\n")
        monkeypatch.setattr(config, "HOME_DIR", tmp_path)
        monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "user.session")
        config.load_dotenv()
        assert os.environ["TELESHIELD_API_ID"] == "12345"
        assert os.environ["TELESHIELD_API_HASH"] == "abc123"
        # 清理避免污染其他測試
        os.environ.pop("TELESHIELD_API_ID", None)
        os.environ.pop("TELESHIELD_API_HASH", None)
