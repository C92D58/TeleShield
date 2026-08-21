"""community 名單 import/export + report HTML 測試。"""

import asyncio
import json

import pytest

import teleshield.commands as commands
import teleshield.config as config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "BLOCK_LOG", tmp_path / "block_log.json")
    monkeypatch.setattr(commands, "HOME_DIR", tmp_path)
    commands.HOME_DIR = tmp_path
    return tmp_path


class TestListImportExport:
    def test_export_format(self, isolated_home, capsys):
        config.save_config({"blacklist": {"123": {"reason": "x"}}})
        out = isolated_home / "export.json"
        asyncio.run(commands.manage_list("export", "blacklist", str(out)))
        data = json.loads(out.read_text())
        assert data["users"] == ["123"]
        assert data["type"] == "blacklist"

    def test_import(self, isolated_home, capsys):
        f = isolated_home / "community.json"
        f.write_text(json.dumps({"users": ["1", "2", "3"]}))
        asyncio.run(commands.manage_list("import", "blacklist", str(f)))
        cfg = config.load_config()
        assert set(cfg["blacklist"].keys()) == {"1", "2", "3"}
        assert cfg["blacklist"]["1"]["reason"] == "community"

    def test_import_dedup(self, isolated_home, capsys):
        f = isolated_home / "community.json"
        f.write_text(json.dumps({"users": ["1", "1", "2"]}))
        asyncio.run(commands.manage_list("import", "blacklist", str(f)))
        cfg = config.load_config()
        assert set(cfg["blacklist"].keys()) == {"1", "2"}

    def test_import_invalid_file(self, isolated_home, capsys):
        f = isolated_home / "bad.json"
        f.write_text("not json")
        asyncio.run(commands.manage_list("import", "blacklist", str(f)))
        assert "失敗" in capsys.readouterr().out


class TestReportHTML:
    def test_html_report_generated(self, isolated_home, capsys):
        # 造封鎖記錄
        for i in range(5):
            config.log_block(100 + i, f"user{i}", "賭博廣告", "private")
        asyncio.run(commands.report("week", output_html=True))
        out = isolated_home / "report_week.html"
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "TeleShield 報告" in content
        assert "賭博" in content
        assert "ff9e5e" in content  # 琥珀色調

    def test_html_no_blocks(self, isolated_home, capsys):
        asyncio.run(commands.report("week", output_html=True))
        assert "尚無封鎖記錄" in capsys.readouterr().out
