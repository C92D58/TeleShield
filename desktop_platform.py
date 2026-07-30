"""Cross-platform login/startup integration for the TeleShield desktop app."""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

STARTUP_ID = "org.teleshield.desktop"
STARTUP_NAME = "TeleShield"


def application_command() -> list[str]:
    """Return the command used by the OS to relaunch the desktop app."""
    if sys.platform == "darwin" and os.getenv("TELESHIELD_STARTUP_APP"):
        return ["/usr/bin/open", "-a", os.environ["TELESHIELD_STARTUP_APP"], "--args", "--background"]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    app_path = Path(__file__).resolve().with_name("desktop_app.py")
    return [sys.executable, str(app_path), "--background"]


def startup_file_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{STARTUP_ID}.plist"
    if sys.platform == "win32":
        # Windows stores this in the registry; the path is only used by tests
        # and diagnostics on that platform.
        return Path.home() / "AppData" / "Roaming" / STARTUP_ID
    return Path.home() / ".config" / "autostart" / f"{STARTUP_ID}.desktop"


def _set_macos_startup(enabled: bool) -> None:
    path = startup_file_path()
    uid = str(os.getuid())
    domain = f"gui/{uid}"

    # Remove a previously bootstrapped agent first. It is fine if it was not
    # loaded; launchctl returns a non-zero status in that case.
    subprocess.run(
        ["launchctl", "bootout", domain, str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not enabled:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": STARTUP_ID,
        "ProgramArguments": application_command(),
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError("macOS launchctl 無法註冊開機啟動項目")


def _set_windows_startup(enabled: bool) -> None:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
    ) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                STARTUP_NAME,
                0,
                winreg.REG_SZ,
                subprocess.list2cmdline(application_command()),
            )
        else:
            try:
                winreg.DeleteValue(key, STARTUP_NAME)
            except FileNotFoundError:
                pass


def _set_linux_startup(enabled: bool) -> None:
    path = startup_file_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.join(application_command())
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={STARTUP_NAME}\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def set_start_on_login(enabled: bool) -> None:
    """Enable or disable launching TeleShield when the user logs in."""
    if sys.platform == "darwin":
        _set_macos_startup(enabled)
    elif sys.platform == "win32":
        _set_windows_startup(enabled)
    else:
        _set_linux_startup(enabled)


def is_start_on_login_enabled() -> bool:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_QUERY_VALUE,
            ) as key:
                winreg.QueryValueEx(key, STARTUP_NAME)
                return True
        except (FileNotFoundError, OSError):
            return False
    return startup_file_path().exists()
