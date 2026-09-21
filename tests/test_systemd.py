"""systemd 模組測試（完全離線，不碰真的 systemd 與真的 ~/.config）。

策略：
- 用 autouse fixture 把 `systemd.HOME_DIR` 與 `Path.home()` 都指向 tmp_path，
  任何路徑推算都落在測試沙箱內（★ 絕不能寫到使用者真的 ~/.config）。
- `systemctl` 一律用 monkeypatch 模擬（`shutil.which` + `subprocess.run`），
  容器裡沒有 systemd 也要能跑。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from teleshield import systemd


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """把資料目錄與家目錄都關進 tmp_path。"""
    fake_home = tmp_path / "home"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(systemd, "HOME_DIR", data_dir)
    # ★ 為什麼要改 Path.home：使用者級安裝預設寫到 ~/.config/...，
    #   不換掉就會污染（甚至真的修改）執行測試的帳號設定
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return {"tmp": tmp_path, "home": fake_home, "data": data_dir}


def _tree(root: Path) -> list[str]:
    """列出目錄底下所有檔案（用來驗證「沒留下任何檔案」）。"""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink())


def _fake_systemctl(*, active="active", enabled="enabled", exc=None):
    """造一個假的 subprocess.run，依 verb 回傳 is-active / is-enabled 的輸出。"""

    def run(argv, **kwargs):
        verb = argv[-2]
        if exc is not None:
            raise exc
        if verb == "is-active":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{active}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{enabled}\n", stderr="")

    return run


def _fake_systemctl_streams(*, active_stdout="", active_stderr="", enabled_stdout="", enabled_stderr=""):
    """更細緻的假 systemctl：可分別指定 stdout / stderr（模擬 unit 不存在等情況）。"""

    def run(argv, **kwargs):
        verb = argv[-2]
        if verb == "is-active":
            return subprocess.CompletedProcess(argv, 3, stdout=active_stdout, stderr=active_stderr)
        return subprocess.CompletedProcess(argv, 1, stdout=enabled_stdout, stderr=enabled_stderr)

    return run


def _lines(unit: str, prefix: str) -> list[str]:
    return [line for line in unit.splitlines() if line.startswith(prefix)]


class TestRenderUnit:
    def test_restart_policy_and_limits(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"))
        assert "Restart=always" in unit
        assert "RestartSec=" in unit
        # 崩潰循環保護（避免開機時網路還沒好就把重啟次數用完）
        assert "StartLimitIntervalSec=" in unit
        assert "StartLimitBurst=" in unit

    def test_execstart_defaults_to_current_interpreter(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"))
        exec_line = _lines(unit, "ExecStart=")[0]
        assert sys.executable in exec_line
        assert exec_line.endswith("-m teleshield --listen")

    def test_execstart_override_is_used_verbatim(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"), exec_start="/usr/local/bin/nope --listen")
        assert "ExecStart=/usr/local/bin/nope --listen" in unit

    def test_type_and_wantedby(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"))
        assert "Type=simple" in unit
        assert "WantedBy=default.target" in unit
        assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit

    def test_hardening_directives(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"))
        assert "NoNewPrivileges=yes" in unit
        assert "PrivateTmp=yes" in unit
        assert "ProtectSystem=full" in unit
        assert "ProtectHome=read-only" in unit
        assert "ReadWritePaths=" in unit

    def test_environment_and_working_directory_follow_home(self):
        unit = systemd.render_unit(home=Path("/tmp/tsdata"))
        assert "Environment=TELESHIELD_HOME=/tmp/tsdata" in unit
        assert "WorkingDirectory=/tmp/tsdata" in unit
        assert "ReadWritePaths=/tmp/tsdata" in unit

    def test_default_home_comes_from_config(self, sandbox):
        unit = systemd.render_unit()
        assert f"Environment=TELESHIELD_HOME={sandbox['data']}" in unit

    def test_user_and_group_only_when_requested(self):
        assert _lines(systemd.render_unit(home=Path("/tmp/ts")), "User=") == []
        unit = systemd.render_unit(home=Path("/tmp/ts"), user="teleshield")
        assert "User=teleshield" in unit
        assert "Group=teleshield" in unit

    def test_extra_env_lines_are_sorted_and_quoted(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"), extra_env={"TELESHIELD_B": "2", "TELESHIELD_A": "1"})
        assert unit.index("Environment=TELESHIELD_A=1") < unit.index("Environment=TELESHIELD_B=2")
        spaced = systemd.render_unit(home=Path("/tmp/ts"), extra_env={"TELESHIELD_MSG": "hello world"})
        assert 'Environment="TELESHIELD_MSG=hello world"' in spaced

    def test_extra_env_cannot_override_home(self):
        unit = systemd.render_unit(home=Path("/tmp/ts"), extra_env={"TELESHIELD_HOME": "/evil"})
        assert "/evil" not in unit
        assert unit.count("Environment=TELESHIELD_HOME=") == 1

    def test_specifier_percent_is_escaped(self):
        # ★ systemd 會展開 %（specifier），路徑含 % 必須寫成 %% 才不會被吃掉
        unit = systemd.render_unit(home=Path("/tmp/my data%dir"))
        assert 'WorkingDirectory="/tmp/my data%%dir"' in unit


class TestRenderLogrotate:
    def test_required_directives(self):
        text = systemd.render_logrotate(home=Path("/tmp/ts"))
        for directive in ("daily", "rotate", "copytruncate", "compress", "missingok", "notifempty"):
            assert directive in text

    def test_targets_home_logs_and_keep_days(self):
        text = systemd.render_logrotate(home=Path("/tmp/ts"), keep_days=7)
        assert "/tmp/ts/logs/*.log" in text
        assert "rotate 7" in text

    def test_keep_days_is_clamped(self):
        assert "rotate 1" in systemd.render_logrotate(home=Path("/tmp/ts"), keep_days=0)
        assert "rotate 14" in systemd.render_logrotate(home=Path("/tmp/ts"), keep_days="oops")


class TestInstall:
    def test_dry_run_writes_nothing_to_disk(self, sandbox):
        tmp = sandbox["tmp"]
        result = systemd.install(home=sandbox["data"], dry_run=True)
        assert result["wrote"] == []
        assert _tree(tmp) == []  # 連目錄都不該被建立
        assert not result["unit_path"].exists()
        assert not result["logrotate_path"].exists()
        assert not sandbox["data"].exists()

    def test_dry_run_returns_complete_payload(self, sandbox):
        result = systemd.install(home=sandbox["data"], dry_run=True)
        assert {"unit_path", "logrotate_path", "unit", "logrotate", "wrote"} <= set(result)
        assert isinstance(result["unit_path"], Path)
        assert isinstance(result["logrotate_path"], Path)
        assert "Restart=always" in result["unit"]
        assert "copytruncate" in result["logrotate"]

    def test_user_level_paths(self, sandbox):
        result = systemd.install(home=sandbox["data"], dry_run=True)
        assert result["unit_path"] == sandbox["home"] / ".config" / "systemd" / "user" / "teleshield.service"
        # logrotate 使用者級不吃 ~/.config/logrotate.d，改放資料目錄
        assert result["logrotate_path"] == sandbox["data"] / "logrotate.conf"
        assert "logrotate" in result["note"]

    def test_system_level_paths(self, sandbox):
        result = systemd.install(system=True, home=sandbox["data"], dry_run=True)
        assert result["unit_path"] == Path("/etc/systemd/system/teleshield.service")
        assert result["logrotate_path"] == Path("/etc/logrotate.d/teleshield")

    def test_user_level_actually_writes_and_chmods(self, sandbox):
        result = systemd.install(home=sandbox["data"])
        unit_path = result["unit_path"]
        assert result["wrote"] == [unit_path, result["logrotate_path"]]
        assert unit_path.read_text(encoding="utf-8") == result["unit"]
        assert result["logrotate_path"].read_text(encoding="utf-8") == result["logrotate"]
        # ★ service 檔不該是 644（內含路徑等資訊）
        assert unit_path.stat().st_mode & 0o777 == 0o600
        assert result["logrotate_path"].stat().st_mode & 0o777 == 0o600
        # 資料目錄內含 session（等同帳號控制權），應為 700
        assert sandbox["data"].stat().st_mode & 0o777 == 0o700
        # 不留暫存檔
        assert not [p for p in sandbox["home"].rglob("*.tmp")]
        # 不該偷寫 ~/.config/logrotate.d
        assert not (sandbox["home"] / ".config" / "logrotate.d").exists()

    def test_system_level_without_root_raises(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "etc" / "teleshield.service")
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        with pytest.raises(PermissionError) as excinfo:
            systemd.install(system=True, home=sandbox["data"])
        assert "root" in str(excinfo.value)
        assert _tree(sandbox["tmp"]) == []  # 失敗時不留半殘檔案

    def test_write_failure_is_wrapped_and_rolled_back(self, sandbox, monkeypatch):
        real_write = systemd._atomic_write_text

        def flaky(path, text):
            if path.name == "logrotate.conf":  # 第二個檔案寫失敗 → unit 應被回滾
                raise PermissionError(f"拒絕寫入 {path}")
            real_write(path, text)

        monkeypatch.setattr(systemd, "_atomic_write_text", flaky)
        with pytest.raises(PermissionError):
            systemd.install(home=sandbox["data"])
        assert not (sandbox["home"] / ".config" / "systemd" / "user" / "teleshield.service").exists()


class TestUninstall:
    def test_missing_files_are_skipped(self, sandbox):
        assert systemd.uninstall(home=sandbox["data"]) == []

    def test_removes_installed_files(self, sandbox):
        result = systemd.install(home=sandbox["data"])
        removed = systemd.uninstall(home=sandbox["data"])
        assert removed == [result["unit_path"], result["logrotate_path"]]
        assert not result["unit_path"].exists()
        assert not result["logrotate_path"].exists()

    def test_removes_enable_symlink(self, sandbox):
        unit_path = sandbox["home"] / ".config" / "systemd" / "user" / "teleshield.service"
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text("[Unit]\n", encoding="utf-8")
        wants = unit_path.parent / "default.target.wants"
        wants.mkdir()
        link = wants / "teleshield.service"
        link.symlink_to(unit_path)

        removed = systemd.uninstall(home=sandbox["data"])
        assert unit_path in removed
        assert link in removed
        assert not link.is_symlink()

    def test_system_level_uninstall_is_graceful(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd, "_logrotate_path", lambda system, home: sandbox["tmp"] / "nope.conf")
        assert systemd.uninstall(system=True, home=sandbox["data"]) == []


class TestStatus:
    def test_missing_systemctl_is_graceful(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: None)
        result = systemd.status()
        assert result["installed"] is False
        assert result["active"] is None
        assert result["enabled"] is None
        assert set(result) == {"installed", "active", "enabled", "detail"}
        assert "systemctl" in result["detail"]

    def test_active_service_reported(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: "/usr/bin/systemctl")
        monkeypatch.setattr(systemd.subprocess, "run", _fake_systemctl(active="active", enabled="enabled"))
        result = systemd.status(system=True)
        assert result["active"] == "active"
        assert result["enabled"] == "enabled"
        assert result["installed"] is True

    def test_disabled_service_gets_enable_hint(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: "/usr/bin/systemctl")
        monkeypatch.setattr(systemd.subprocess, "run", _fake_systemctl(active="inactive", enabled="disabled"))
        result = systemd.status()
        assert result["active"] == "inactive"
        assert "systemctl --user enable --now teleshield" in result["detail"]

    def test_unknown_unit_maps_to_not_found(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: "/usr/bin/systemctl")
        monkeypatch.setattr(
            systemd.subprocess,
            "run",
            _fake_systemctl_streams(
                active_stdout="inactive\n",
                enabled_stderr="Failed to get unit file state for teleshield.service: No such file or directory\n",
            ),
        )
        result = systemd.status()
        assert result["enabled"] == "not-found"
        assert result["installed"] is False
        assert "不存在" in result["detail"]

    def test_bus_failure_is_not_mistaken_for_not_found(self, sandbox, monkeypatch):
        # ★ 「Failed to connect to bus: No such file or directory」也含 no such file，
        #   不能因此回報成 not-found
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: "/usr/bin/systemctl")
        monkeypatch.setattr(
            systemd.subprocess,
            "run",
            _fake_systemctl_streams(
                active_stderr="Failed to connect to bus: No such file or directory\n",
                enabled_stderr="Failed to connect to bus: No such file or directory\n",
            ),
        )
        result = systemd.status()
        assert result["active"] is None
        assert result["enabled"] is None
        assert "查不到結果" in result["detail"]

    def test_subprocess_errors_never_raise(self, sandbox, monkeypatch):
        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: "/usr/bin/systemctl")
        monkeypatch.setattr(systemd.subprocess, "run", _fake_systemctl(exc=FileNotFoundError("no systemctl")))
        assert systemd.status()["installed"] is False

        monkeypatch.setattr(
            systemd.subprocess,
            "run",
            _fake_systemctl(exc=subprocess.TimeoutExpired(cmd="systemctl", timeout=systemd.SYSTEMCTL_TIMEOUT)),
        )
        assert systemd.status()["active"] is None

    def test_systemctl_is_called_with_user_flag_only_when_needed(self, sandbox, monkeypatch):
        seen = []

        def run(argv, **kwargs):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")

        monkeypatch.setattr(systemd, "_unit_path", lambda system: sandbox["tmp"] / "nope.service")
        monkeypatch.setattr(systemd.shutil, "which", lambda *args, **kwargs: "/usr/bin/systemctl")
        monkeypatch.setattr(systemd.subprocess, "run", run)

        systemd.status()
        systemd.status(system=True)
        assert ["--user", "is-active", "teleshield"] == seen[0][1:]
        assert ["is-active", "teleshield"] == seen[2][1:]
        assert all(call[-1] == systemd.UNIT_NAME for call in seen)
