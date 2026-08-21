"""配置、存儲與路徑管理。

- 路徑：`TELESHIELD_HOME` 環境變數（默認 `~/.teleshield`），session/config/日誌全在內
- .env：支援從 `TELESHIELD_HOME/.env` 與項目根 `.env` 載入憑證
- config.json / block_log.json / learned_patterns.json 的讀寫
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "HOME_DIR",
    "SESSION_FILE",
    "CONFIG_FILE",
    "BLOCK_LOG",
    "LEARNED_FILE",
    "load_dotenv",
    "load_config",
    "save_config",
    "load_block_log",
    "save_block_log",
    "log_block",
    "load_learned_patterns",
    "save_learned_patterns",
    "secure_session_file",
    "is_blacklisted",
    "is_whitelisted",
]

HOME_DIR = Path(os.getenv("TELESHIELD_HOME", str(Path.home() / ".teleshield")))
SESSION_FILE = HOME_DIR / "user.session"
CONFIG_FILE = HOME_DIR / "config.json"
BLOCK_LOG = HOME_DIR / "block_log.json"
LEARNED_FILE = HOME_DIR / "learned_patterns.json"


def load_dotenv() -> None:
    """從 TELESHIELD_HOME/.env 或項目根 .env 載入 KEY=VALUE（零依賴實現）。

    已存在的環境變數優先，不覆蓋。僅用於載入 TELESHIELD_* 憑證。
    """
    candidates = [HOME_DIR / ".env", Path.cwd() / ".env"]
    for env_file in candidates:
        if not env_file.exists():
            continue
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


def _atomic_write(path: Path, data) -> None:
    """原子寫入（tmp + rename），並收緊權限為 600（含敏感數據）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def secure_session_file() -> None:
    """收緊 Telethon session 文件權限為 600。

    Telethon 的 SQLiteSession 創建文件時不設權限（默認 umask 644），
    而 session 內含 Telegram 登入憑證（auth key，等價帳號控制權）。
    必須在每次 client.start() 後調用。
    """
    if SESSION_FILE.exists():
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_FILE, cfg)


def load_block_log() -> dict:
    if BLOCK_LOG.exists():
        try:
            return json.loads(BLOCK_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"blocks": []}
    return {"blocks": []}


def save_block_log(log: dict) -> None:
    _atomic_write(BLOCK_LOG, log)


def log_block(user_id: int, name: str, reason: str, source: str = "private") -> None:
    """記錄封鎖事件（保留最近 500 筆）。"""
    log = load_block_log()
    log["blocks"].append(
        {
            "user_id": user_id,
            "name": name,
            "reason": reason[:200],
            "source": source,
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )
    if len(log["blocks"]) > 500:
        log["blocks"] = log["blocks"][-500:]
    save_block_log(log)


def load_learned_patterns() -> dict:
    if LEARNED_FILE.exists():
        try:
            return json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"keywords": [], "patterns": []}
    return {"keywords": [], "patterns": []}


def save_learned_patterns(data: dict) -> None:
    _atomic_write(LEARNED_FILE, data)


def is_blacklisted(user_id: int, cfg: dict) -> bool:
    return str(user_id) in cfg.get("blacklist", {})


def is_whitelisted(user_id: int, cfg: dict) -> bool:
    return str(user_id) in cfg.get("whitelist", {})
