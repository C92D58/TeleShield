#!/usr/bin/env python3
"""
Telegram 廣告封鎖工具 — TeleShield 完整版
────────────────────────────────────
用法：
  --setup [api_id] [api_hash] [phone] [code]  首次設定
  --scan                                       掃描並封鎖
  --dry-run                                    試掃描
  --listen                                     即時監聽（後台常駐）
  --status                                     查看狀態
  --report [day|week]                          封鎖摘要報告
  --learn <廣告文字>                            手動標記學習新模式
  --whitelist add|remove|list <user_id>        白名單管理
  --blacklist add|remove|list <user_id>        黑名單管理
  --group-scan                                 掃描群組並踢除廣告
"""

import asyncio, csv, errno, json, os, random, re, shutil, sys, tempfile, threading, time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from telethon import events
from collections import defaultdict

# ──────────── 設定 ────────────

def default_session_dir() -> Path:
    """Return a writable per-user data directory on every supported OS."""
    override = os.getenv("TELESHIELD_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
        return root / "TeleShield"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TeleShield"

    root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share"))
    return root / "TeleShield"


SESSION_DIR = default_session_dir()
SESSION_FILE = SESSION_DIR / "user.session"
CONFIG_FILE = SESSION_DIR / "config.json"
BLOCK_LOG = SESSION_DIR / "block_log.json"


class AccountStore:
    """Filesystem paths belonging to exactly one Telegram account."""

    def __init__(
        self,
        root: Path,
        session_file: Optional[Path] = None,
        config_file: Optional[Path] = None,
        block_log: Optional[Path] = None,
        account_id: Optional[str] = None,
        data_root: Optional[Path] = None,
    ):
        self.root = Path(root)
        self.account_id = account_id
        self.data_root = Path(data_root) if data_root is not None else (
            self.root.parent.parent if account_id else self.root
        )
        self.session_file = Path(session_file) if session_file is not None else self.root / "user.session"
        self.config_file = Path(config_file or self.root / "config.json")
        self.block_log = Path(block_log or self.root / "block_log.json")
        self.learned_patterns_file = self.root / "learned_patterns.json"

    def ensure(self) -> None:
        directories = [self.data_root]
        if self.account_id:
            directories.append(self.data_root / "accounts")
        directories.append(self.root)
        for directory in directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        for path in (
            self.session_file,
            Path(f"{self.session_file}-journal"),
            Path(f"{self.session_file}-wal"),
            Path(f"{self.session_file}-shm"),
            self.config_file,
            self.block_log,
            self.learned_patterns_file,
        ):
            if not path.exists():
                continue
            path.chmod(0o600)
        defaults = (
            (self.config_file, {}),
            (self.block_log, {"blocks": []}),
            (self.learned_patterns_file, {"keywords": [], "patterns": []}),
        )
        for path, data in defaults:
            if path.exists():
                continue
            _write_private_bytes(
                path,
                json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
            )


_CURRENT_ACCOUNT_STORE: ContextVar[Optional[AccountStore]] = ContextVar(
    "teleshield_current_account_store",
    default=None,
)
_ACCOUNT_REGISTRY_LOCK = threading.RLock()
_ACCOUNT_REGISTRY_LOCK_STATE = threading.local()
_ACCOUNT_SESSION_MUTEX_GUARD = threading.Lock()
_ACCOUNT_SESSION_MUTEXES = {}
_ACCOUNT_SESSION_LOCK_STATE = threading.local()


class AccountSessionBusyError(RuntimeError):
    """Raised when another operation owns an account's Telegram Session."""


def _account_session_mutex(key: str):
    with _ACCOUNT_SESSION_MUTEX_GUARD:
        return _ACCOUNT_SESSION_MUTEXES.setdefault(key, threading.RLock())


@contextmanager
def _private_file_lock(path: Path, blocking: bool = True):
    """Hold one cross-platform, owner-only advisory file lock."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    acquired = False
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        else:
            path.chmod(0o600)
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), flags)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def _account_registry_transaction(root: Optional[Path] = None):
    """Serialize a complete registry read/check/write transaction across processes."""
    root_path = _account_data_root(root)
    key = str(root_path.resolve())
    with _ACCOUNT_REGISTRY_LOCK:
        held = getattr(_ACCOUNT_REGISTRY_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _ACCOUNT_REGISTRY_LOCK_STATE.held = held
        if key in held:
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        with _private_file_lock(root_path / ".accounts.lock"):
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)


@contextmanager
def _account_session_lease(
    account_id: Optional[str] = None,
    store: Optional[AccountStore] = None,
    blocking: bool = False,
):
    """Exclusively lease one account's Session across threads and processes."""
    target = store or _resolve_account_store(account_id)
    target.ensure()
    key = str(target.root.resolve())
    mutex = _account_session_mutex(key)
    if not mutex.acquire(blocking=blocking):
        raise AccountSessionBusyError("此 Telegram 帳號正在使用中，請先停止其他操作")
    depths = getattr(_ACCOUNT_SESSION_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _ACCOUNT_SESSION_LOCK_STATE.depths = depths
    try:
        if key in depths:
            depths[key] += 1
            try:
                yield target
            finally:
                depths[key] -= 1
            return
        try:
            lock_id = _validate_account_id(target.account_id) if target.account_id else "legacy"
            lock_path = target.data_root / ".session-locks" / f"{lock_id}.lock"
            with _private_file_lock(lock_path, blocking=blocking):
                depths[key] = 1
                try:
                    yield target
                finally:
                    depths.pop(key, None)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise AccountSessionBusyError("此 Telegram 帳號正在使用中，請先停止其他操作") from exc
            raise
    finally:
        mutex.release()


def _session_leased(function):
    """Wrap an async Telegram operation in one account context and Session lease."""
    function_signature = signature(function)

    @wraps(function)
    async def leased(*args, **kwargs):
        arguments = function_signature.bind_partial(*args, **kwargs).arguments
        with account_context(arguments.get("account_id")) as store:
            with _account_session_lease(store=store):
                return await function(*args, **kwargs)

    return leased


def _registry_locked(function):
    function_signature = signature(function)

    @wraps(function)
    def locked(*args, **kwargs):
        arguments = function_signature.bind_partial(*args, **kwargs).arguments
        with _account_registry_transaction(arguments.get("root")):
            return function(*args, **kwargs)
    return locked


def _account_data_root(root: Optional[Path] = None) -> Path:
    return Path(root) if root is not None else Path(SESSION_DIR)


def _account_registry_path(root: Optional[Path] = None) -> Path:
    return _account_data_root(root) / "accounts.json"


def _empty_account_registry() -> dict:
    return {
        "version": 1,
        "active_account_id": None,
        "auto_start_account_id": None,
        "auto_start_account_configured": False,
        "accounts": [],
    }


@_registry_locked
def _read_account_registry(root: Optional[Path] = None) -> dict:
    path = _account_registry_path(root)
    if not path.exists():
        return _empty_account_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"帳號索引無法讀取：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("帳號索引格式無效")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    return {
        "version": int(data.get("version", 1)),
        "active_account_id": data.get("active_account_id"),
        "auto_start_account_id": data.get("auto_start_account_id"),
        "auto_start_account_configured": bool(
            data.get("auto_start_account_configured", "auto_start_account_id" in data)
        ),
        "accounts": [dict(item) for item in accounts if isinstance(item, dict)],
    }


@_registry_locked
def _write_account_registry(data: dict, root: Optional[Path] = None) -> None:
    path = _account_registry_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp_file = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        tmp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.chmod(0o600)
        os.replace(tmp_file, path)
        path.chmod(0o600)
    finally:
        try:
            tmp_file.unlink()
        except FileNotFoundError:
            pass


def _write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically replace one owner-only file without a permissive creation window."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.restore-{uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_private_file(path: Path, snapshot: Optional[bytes]) -> None:
    if snapshot is None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        return
    _write_private_bytes(Path(path), snapshot)


def _validate_account_id(account_id: str) -> str:
    account_id = str(account_id or "").strip()
    if not account_id or account_id in {".", ".."} or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in account_id):
        raise ValueError("帳號 ID 格式無效")
    return account_id


def account_store(account_id: str, root: Optional[Path] = None) -> AccountStore:
    account_id = _validate_account_id(account_id)
    data_root = _account_data_root(root)
    return AccountStore(data_root / "accounts" / account_id, account_id=account_id, data_root=data_root)


def _legacy_account_store() -> AccountStore:
    return AccountStore(SESSION_DIR, SESSION_FILE, CONFIG_FILE, BLOCK_LOG, data_root=SESSION_DIR)


def _resolve_account_store(account_id: Optional[str] = None) -> AccountStore:
    if account_id:
        return account_store(account_id)
    current = _CURRENT_ACCOUNT_STORE.get()
    if current is not None:
        return current
    active_account_id = get_active_account_id()
    if active_account_id:
        return account_store(active_account_id)
    return _legacy_account_store()


@contextmanager
def account_context(account_id: Optional[str] = None, store: Optional[AccountStore] = None):
    """Bind all implicit core storage calls to one account for this task/thread."""
    target = store or _resolve_account_store(account_id)
    token = _CURRENT_ACCOUNT_STORE.set(target)
    try:
        yield target
    finally:
        _CURRENT_ACCOUNT_STORE.reset(token)


def _mask_phone(phone: str) -> str:
    phone = str(phone or "")
    if len(phone) <= 4:
        return "" if not phone else "*" * len(phone)
    return f"{phone[:3]}{'*' * max(1, len(phone) - 5)}{phone[-2:]}"


@_registry_locked
def list_accounts(root: Optional[Path] = None) -> list:
    return list(_read_account_registry(root).get("accounts", []))


@_registry_locked
def get_account(account_id: str, root: Optional[Path] = None) -> Optional[dict]:
    account_id = _validate_account_id(account_id)
    return next((record for record in list_accounts(root) if str(record.get("id")) == account_id), None)


@_registry_locked
def get_active_account_id(root: Optional[Path] = None) -> Optional[str]:
    registry = _read_account_registry(root)
    active = registry.get("active_account_id")
    if not active:
        return None
    if any(str(item.get("id")) == str(active) for item in registry.get("accounts", [])):
        return str(active)
    return None


@_registry_locked
def create_account(account_id: Optional[str] = None, root: Optional[Path] = None, metadata: Optional[dict] = None) -> dict:
    root_path = _account_data_root(root)
    registry = _read_account_registry(root_path)
    account_id = _validate_account_id(account_id or f"account-{uuid4().hex[:12]}")
    existing = next((item for item in registry["accounts"] if str(item.get("id")) == account_id), None)
    if existing is not None:
        account_store(account_id, root_path).ensure()
        return dict(existing)
    _assert_unique_account_identity(account_id, (metadata or {}).get("user_id"), root_path)
    store = account_store(account_id, root_path)
    store.ensure()
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": account_id,
        "user_id": None,
        "username": "",
        "display_name": "",
        "phone_masked": "",
        "created_at": now,
        "last_used_at": now,
        "auto_start_protection": False,
    }
    for key in record:
        if metadata and key in metadata and key not in {"id", "created_at"}:
            record[key] = metadata[key]
    store.ensure()
    registry["accounts"].append(record)
    if not registry.get("active_account_id"):
        registry["active_account_id"] = account_id
    _write_account_registry(registry, root_path)
    return dict(record)


@_registry_locked
def set_active_account(account_id: str, root: Optional[Path] = None) -> dict:
    root_path = _account_data_root(root)
    registry = _read_account_registry(root_path)
    account_id = _validate_account_id(account_id)
    record = next((item for item in registry["accounts"] if str(item.get("id")) == account_id), None)
    if record is None:
        raise ValueError("找不到指定 Telegram 帳號")
    record["last_used_at"] = datetime.now(timezone.utc).isoformat()
    registry["active_account_id"] = account_id
    _write_account_registry(registry, root_path)
    return dict(record)


def _config_requests_legacy_auto_start(account_id: str) -> bool:
    try:
        return bool(load_config(account_id).get("auto_start_protection"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


@_registry_locked
def get_auto_start_account_id(root: Optional[Path] = None) -> Optional[str]:
    """Return the one account selected for automatic protection at app startup."""
    registry = _read_account_registry(root)
    account_ids = {str(item.get("id")) for item in registry.get("accounts", []) if item.get("id")}
    selected = registry.get("auto_start_account_id")
    if registry.get("auto_start_account_configured"):
        if selected:
            try:
                selected = _validate_account_id(str(selected))
            except ValueError:
                return None
            if selected in account_ids:
                return selected
        return None
    # Backward compatibility for registries created before the global selector.
    legacy = []
    for item in registry.get("accounts", []):
        raw_id = item.get("id")
        if not raw_id:
            continue
        try:
            candidate_id = _validate_account_id(str(raw_id))
        except ValueError:
            continue
        if item.get("auto_start_protection") or _config_requests_legacy_auto_start(candidate_id):
            legacy.append(candidate_id)
    active = str(registry.get("active_account_id")) if registry.get("active_account_id") else ""
    if active in legacy:
        return active
    return legacy[0] if legacy else None


@_registry_locked
def set_auto_start_account(account_id: Optional[str], root: Optional[Path] = None) -> Optional[str]:
    """Persist the single account whose protection should start with the app."""
    root_path = _account_data_root(root)
    registry = _read_account_registry(root_path)
    selected = None if account_id in (None, "") else _validate_account_id(str(account_id))
    if selected and not any(str(item.get("id")) == selected for item in registry["accounts"]):
        raise ValueError("找不到指定 Telegram 帳號")
    registry["auto_start_account_id"] = selected
    registry["auto_start_account_configured"] = True
    _write_account_registry(registry, root_path)
    return selected


@_registry_locked
def _assert_unique_account_identity(account_id: str, user_id, root: Optional[Path] = None) -> None:
    if user_id is None:
        return
    account_id = _validate_account_id(account_id)
    for item in list_accounts(root):
        if str(item.get("id")) == account_id:
            continue
        existing_user_id = item.get("user_id")
        if existing_user_id is not None and str(existing_user_id) == str(user_id):
            raise ValueError("這個 Telegram 帳號已經存在，不能建立第二個共用 Session")


@_registry_locked
def update_account_identity(account_id: str, me, phone: str = "", root: Optional[Path] = None) -> dict:
    root_path = _account_data_root(root)
    account_id = _validate_account_id(account_id)
    registry = _read_account_registry(root_path)
    record = next((item for item in registry["accounts"] if str(item.get("id")) == account_id), None)
    if record is None:
        record = create_account(account_id, root_path)
        registry = _read_account_registry(root_path)
        record = next(item for item in registry["accounts"] if str(item.get("id")) == account_id)
    user_id = getattr(me, "id", None)
    _assert_unique_account_identity(account_id, user_id, root_path)
    record.update({
        "user_id": user_id,
        "username": getattr(me, "username", None) or "",
        "display_name": " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")])).strip(),
        "phone_masked": _mask_phone(phone),
        "last_used_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_account_registry(registry, root_path)
    return dict(record)


@_registry_locked
def _commit_account_identity_and_config(
    account_id: str,
    me,
    phone: str,
    cfg: dict,
    root: Optional[Path] = None,
) -> dict:
    """Commit identity metadata and config as one rollback-capable registry transaction."""
    root_path = _account_data_root(root)
    account_id = _validate_account_id(account_id)
    store = account_store(account_id, root_path)
    store.ensure()
    registry_path = _account_registry_path(root_path)
    registry_snapshot = registry_path.read_bytes() if registry_path.exists() else None
    config_snapshot = store.config_file.read_bytes() if store.config_file.exists() else None
    try:
        _assert_unique_account_identity(account_id, getattr(me, "id", None), root_path)
        with account_context(store=store):
            save_config(cfg)
        registry = _read_account_registry(root_path)
        record = next((item for item in registry["accounts"] if str(item.get("id")) == account_id), None)
        if record is None:
            raise ValueError("找不到指定 Telegram 帳號")
        record.update({
            "user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None) or "",
            "display_name": " ".join(
                filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")])
            ).strip(),
            "phone_masked": _mask_phone(phone),
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_account_registry(registry, root_path)
        return dict(record)
    except Exception:
        _restore_private_file(store.config_file, config_snapshot)
        _restore_private_file(registry_path, registry_snapshot)
        raise


@_registry_locked
def remove_account(account_id: str, delete_files: bool = True, root: Optional[Path] = None) -> bool:
    root_path = _account_data_root(root)
    account_id = _validate_account_id(account_id)
    registry = _read_account_registry(root_path)
    before = len(registry["accounts"])
    registry["accounts"] = [item for item in registry["accounts"] if str(item.get("id")) != account_id]
    if len(registry["accounts"]) == before:
        return False

    def commit_registry_removal() -> None:
        if registry.get("active_account_id") == account_id:
            registry["active_account_id"] = registry["accounts"][0]["id"] if registry["accounts"] else None
        if registry.get("auto_start_account_id") == account_id:
            registry["auto_start_account_id"] = None
        _write_account_registry(registry, root_path)

    if delete_files:
        store = account_store(account_id, root_path)
        with _account_session_lease(store=store, blocking=False):
            account_root = store.root
            if account_root.exists():
                try:
                    shutil.rmtree(account_root, ignore_errors=False)
                except OSError:
                    return False
                if account_root.exists():
                    return False
            commit_registry_removal()
    else:
        commit_registry_removal()
    return True


@_registry_locked
def _migrate_files_transactionally(
    sources,
    destinations,
    registry: dict,
    root: Path,
    target_root: Path,
    target_existed: bool,
) -> None:
    """Commit migrated files, hidden legacy sources, and registry as one transaction."""
    staged = []
    committed = []
    hidden_sources = []
    registry_path = _account_registry_path(root)
    registry_snapshot = registry_path.read_bytes() if registry_path.exists() else None
    try:
        for source, destination in zip(sources, destinations):
            if not source.exists():
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.parent.chmod(0o700)
            temporary = destination.with_name(f".{destination.name}.migrate-{uuid4().hex}")
            shutil.copy2(source, temporary)
            temporary.chmod(0o600)
            staged.append((source, temporary, destination))

        for _, temporary, destination in staged:
            backup = None
            if destination.exists():
                backup = destination.with_name(f".{destination.name}.backup-{uuid4().hex}")
                os.replace(destination, backup)
                backup.chmod(0o600)
            committed.append((destination, backup))
            os.replace(temporary, destination)
            destination.chmod(0o600)

        for source, _, _ in staged:
            tombstone = source.with_name(f".{source.name}.migrated-{uuid4().hex}")
            os.replace(source, tombstone)
            tombstone.chmod(0o600)
            hidden_sources.append((source, tombstone))

        _write_account_registry(registry, root)
    except Exception as exc:
        rollback_errors = []
        try:
            _restore_private_file(registry_path, registry_snapshot)
        except Exception as rollback_exc:
            rollback_errors.append(rollback_exc)
        for source, tombstone in reversed(hidden_sources):
            try:
                if tombstone.exists():
                    os.replace(tombstone, source)
                    source.chmod(0o600)
            except Exception as rollback_exc:
                rollback_errors.append(rollback_exc)
        for destination, backup in reversed(committed):
            try:
                if destination.exists():
                    destination.unlink()
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
                    destination.chmod(0o600)
            except Exception as rollback_exc:
                rollback_errors.append(rollback_exc)
        for _, temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except Exception as rollback_exc:
                rollback_errors.append(rollback_exc)
        if not target_existed and target_root.exists():
            try:
                shutil.rmtree(target_root, ignore_errors=False)
            except Exception as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise RuntimeError(
                f"舊版資料遷移失敗且 rollback 不完整：{rollback_errors[0]}"
            ) from exc
        raise RuntimeError(f"舊版資料遷移失敗：{exc}") from exc

    cleanup_errors = []
    for _, tombstone in hidden_sources:
        try:
            tombstone.unlink()
        except FileNotFoundError:
            pass
        except Exception as cleanup_exc:
            cleanup_errors.append(cleanup_exc)
    for _, backup in committed:
        if backup is None:
            continue
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        except Exception as cleanup_exc:
            cleanup_errors.append(cleanup_exc)
    if cleanup_errors:
        raise RuntimeError(f"舊版資料已遷移，但敏感暫存檔清理失敗：{cleanup_errors[0]}")


@_registry_locked
def ensure_account_registry(root: Optional[Path] = None) -> list:
    """Create the account registry and migrate the old single-account layout once."""
    root_path = _account_data_root(root)
    registry = _read_account_registry(root_path)
    for directory in (root_path, root_path / "accounts"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    for item in registry["accounts"]:
        raw_id = item.get("id")
        if raw_id:
            account_store(str(raw_id), root_path).ensure()
    legacy = _legacy_account_store() if root is None else AccountStore(
        root_path,
        root_path / "user.session",
        root_path / "config.json",
        root_path / "block_log.json",
        data_root=root_path,
    )
    sources = [
        legacy.session_file,
        Path(f"{legacy.session_file}-journal"),
        legacy.config_file,
        legacy.block_log,
        legacy.learned_patterns_file,
    ]
    if not any(path.exists() for path in sources):
        if registry["accounts"]:
            return list(registry["accounts"])
        _write_account_registry(registry, root_path)
        return []

    legacy_cfg = {}
    if legacy.config_file.exists():
        try:
            legacy_cfg = json.loads(legacy.config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy_cfg = {}
    user_id = legacy_cfg.get("user_id")
    derived_account_id = f"user-{user_id}" if str(user_id or "").lstrip("-").isdigit() else ""
    existing_record = next(
        (
            item for item in registry["accounts"]
            if (derived_account_id and str(item.get("id")) == derived_account_id)
            or (user_id is not None and str(item.get("user_id")) == str(user_id))
        ),
        None,
    )
    if existing_record is None and user_id is None and len(registry["accounts"]) == 1:
        existing_record = registry["accounts"][0]
    account_id = str(existing_record["id"]) if existing_record else (
        derived_account_id or f"account-{uuid4().hex[:12]}"
    )
    if any(str(item.get("id")) == account_id for item in registry["accounts"]) and not existing_record:
        account_id = f"account-{uuid4().hex[:12]}"
    target = account_store(account_id, root_path)
    target_existed = target.root.exists()
    target.ensure()
    target_files = [
        target.session_file,
        Path(f"{target.session_file}-journal"),
        target.config_file,
        target.block_log,
        target.learned_patterns_file,
    ]
    next_registry = {
        **registry,
        "accounts": [dict(item) for item in registry["accounts"]],
    }
    if existing_record is not None:
        record = next(
            item for item in next_registry["accounts"]
            if str(item.get("id")) == account_id
        )
    else:
        _assert_unique_account_identity(account_id, user_id, root_path)
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": account_id,
            "user_id": user_id,
            "username": legacy_cfg.get("username", "") or "",
            "display_name": legacy_cfg.get("display_name", "") or "",
            "phone_masked": _mask_phone(legacy_cfg.get("phone", "")),
            "created_at": now,
            "last_used_at": now,
            "auto_start_protection": bool(legacy_cfg.get("auto_start_protection", False)),
        }
        next_registry["accounts"].append(record)
    next_registry["active_account_id"] = record["id"]
    _migrate_files_transactionally(
        sources,
        target_files,
        next_registry,
        root_path,
        target.root,
        target_existed,
    )
    return list(next_registry["accounts"])
SPAM_PATTERNS = [
    # 中文廣告常見模式
    r"加[\s\-]*[LlvVXx]|[LlvVXx][\s\-]*信",
    r"tg[\s\-]*@?[a-zA-Z0-9_]{3,}",
    r"https?://t\.me/",
    r"@\w{4,}",
    r"兼職|刷單|日入|月入|躺賺|被動收入|在家工作|輕鬆賺",
    r"投資|理財|帶單|跟單|量化|穩賺|穩健|高回報|高收益",
    r"色情|A片|av|成人|裸聊|約炮|援交|包養",
    r"賭|博|彩|casino|betting",
    r"註冊送|免費領|紅包|禮金|優惠碼|推廣碼",
    r"點贊|關注|刷粉|刷讚|漲粉",
    r"售|賣|出|供應|批發|代購|代發",
    # English patterns
    r"promote|promotion|advertisement|sponsor",
    r"click\s*(here|this\s*link|the\s*link)",
    r"earn\s*money|work\s*from\s*home|passive\s*income",
    r"free\s*crypto|free\s*bitcoin|airdrop|giveaway",
    r"limited\s*offer|discount\s*\d{2,}%|buy\s*now",
]

DEFAULT_SCAN_SETTINGS = {
    "private_dialog_limit": 30,
    "private_message_limit": 5,
    "private_days": 14,
    "group_dialog_limit": 50,
    "group_message_limit": 20,
    "group_days": 3,
}

SCAN_SETTING_BOUNDS = {
    "private_dialog_limit": (1, 100),
    "private_message_limit": (1, 100),
    "private_days": (1, 365),
    "group_dialog_limit": (1, 100),
    "group_message_limit": (1, 100),
    "group_days": (1, 365),
}

# ──────────── 工具函式 ────────────

def load_config(account_id: Optional[str] = None):
    config_file = _resolve_account_store(account_id).config_file
    if config_file.exists():
        return json.loads(config_file.read_text(encoding="utf-8"))
    return {}

def save_config(cfg, account_id: Optional[str] = None):
    config_file = _resolve_account_store(account_id).config_file
    config_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private_bytes(
        config_file,
        json.dumps(cfg, indent=2, ensure_ascii=False).encode("utf-8"),
    )

def load_block_log(account_id: Optional[str] = None):
    block_log = _resolve_account_store(account_id).block_log
    if block_log.exists():
        return json.loads(block_log.read_text(encoding="utf-8"))
    return {"blocks": []}

def save_block_log(log, account_id: Optional[str] = None):
    block_log = _resolve_account_store(account_id).block_log
    block_log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private_bytes(
        block_log,
        json.dumps(log, indent=2, ensure_ascii=False).encode("utf-8"),
    )

def load_learned_patterns(account_id: Optional[str] = None):
    f = _resolve_account_store(account_id).learned_patterns_file
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"keywords": [], "patterns": []}

def save_learned_patterns(data, account_id: Optional[str] = None):
    learned_file = _resolve_account_store(account_id).learned_patterns_file
    learned_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private_bytes(
        learned_file,
        json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
    )


def get_learned_patterns(cfg: dict = None, account_id: Optional[str] = None) -> dict:
    cfg = cfg if cfg is not None else load_config(account_id)
    configured = cfg.get("learned_patterns", {}) if isinstance(cfg, dict) else {}
    learned_file = _resolve_account_store(account_id).learned_patterns_file
    stored = {}
    if learned_file.exists():
        stored = json.loads(learned_file.read_text(encoding="utf-8"))
    return {
        "keywords": list(dict.fromkeys([
            *list(configured.get("keywords", [])),
            *list(stored.get("keywords", [])),
        ])),
        "patterns": list(dict.fromkeys([
            *list(configured.get("patterns", [])),
            *list(stored.get("patterns", [])),
        ])),
    }


def remove_learned_pattern(kind: str, value: str, account_id: Optional[str] = None) -> bool:
    if kind not in {"keywords", "patterns"}:
        raise ValueError("kind 必須是 keywords 或 patterns")
    cfg = load_config(account_id)
    learned = get_learned_patterns(cfg, account_id)
    if value not in learned[kind]:
        return False
    learned[kind].remove(value)
    cfg["learned_patterns"] = learned
    save_config(cfg, account_id)
    save_learned_patterns(learned, account_id)
    return True


def get_scan_settings(cfg: dict = None, account_id: Optional[str] = None) -> dict:
    """Return validated scan limits for both GUI and CLI callers."""
    cfg = cfg if cfg is not None else load_config(account_id)
    stored = cfg.get("scan_settings", {})
    settings = {}
    for key, default in DEFAULT_SCAN_SETTINGS.items():
        low, high = SCAN_SETTING_BOUNDS[key]
        try:
            value = int(stored.get(key, default))
        except (TypeError, ValueError):
            value = default
        settings[key] = max(low, min(high, value))
    return settings


def update_scan_settings(updates: dict, account_id: Optional[str] = None) -> dict:
    """Validate and persist user-editable scan limits."""
    cfg = load_config(account_id)
    settings = get_scan_settings(cfg)
    for key, value in (updates or {}).items():
        if key not in SCAN_SETTING_BOUNDS:
            continue
        low, high = SCAN_SETTING_BOUNDS[key]
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        settings[key] = max(low, min(high, value))
    cfg["scan_settings"] = settings
    save_config(cfg, account_id)
    return settings


def learn_text(text: str, account_id: Optional[str] = None) -> dict:
    """Learn a user-supplied spam example without printing to stdout."""
    text = (text or "").strip()
    if not text:
        raise ValueError("請提供要學習的廣告文字")

    cfg = load_config(account_id)
    learned = get_learned_patterns(cfg, account_id)

    tokens = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    stop_words = {
        "我們", "他們", "可以", "沒有", "這個", "那個", "什麼", "因為", "所以", "但是",
        "如果", "雖然", "然後", "而且", "或者", "不過", "還是", "就是", "不是", "一個",
    }
    added_keywords = []
    for token in tokens:
        if token not in stop_words and token not in learned["keywords"]:
            learned["keywords"].append(token)
            added_keywords.append(token)

    added_patterns = []
    match = re.search(r"(加微信|加\s*(?:V|v)|加|薇|威|wechat|line|whatsapp)[-:\s]*([a-zA-Z0-9_]{4,})", text)
    if match:
        pattern = re.escape(match.group(2))
        if pattern not in learned["patterns"]:
            learned["patterns"].append(pattern)
            added_patterns.append(pattern)

    for url in re.findall(r"https?://[^\s]{4,}", text):
        pattern = re.escape(url[:20])
        if pattern not in learned["patterns"]:
            learned["patterns"].append(pattern)
            added_patterns.append(pattern)

    if not added_keywords and not added_patterns:
        pattern = re.escape(text[:30])
        if pattern not in learned["patterns"]:
            learned["patterns"].append(pattern)
            added_patterns.append(pattern)

    cfg["learned_patterns"] = learned
    save_config(cfg, account_id)
    save_learned_patterns(learned, account_id)
    return {
        "text": text,
        "added_keywords": added_keywords,
        "added_patterns": added_patterns,
        "total_keywords": len(learned["keywords"]),
        "total_patterns": len(learned["patterns"]),
    }


def _parse_log_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_report(period: str = "day", now: datetime = None, account_id: Optional[str] = None) -> dict:
    """Return a structured report suitable for a GUI or an export."""
    period = period if period in {"day", "week", "all"} else "day"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if period == "day":
        cutoff = now - timedelta(days=1)
        label = "過去 24 小時"
    elif period == "week":
        cutoff = now - timedelta(days=7)
        label = "過去 7 天"
    else:
        cutoff = datetime.min.replace(tzinfo=timezone.utc)
        label = "全部記錄"

    records = []
    for record in load_block_log(account_id).get("blocks", []):
        try:
            record_time = _parse_log_time(record["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if record_time > cutoff:
            records.append(dict(record))
    records.sort(key=lambda item: item.get("time", ""), reverse=True)

    by_source = defaultdict(int)
    by_reason = defaultdict(int)
    trend = defaultdict(int)
    for record in records:
        raw_source = record.get("source", "private")
        source = "private" if raw_source == "scan" else raw_source
        by_source[source] += 1
        reason = record.get("reason", "")
        by_reason[reason[:20] or "未分類"] += 1
        trend[record.get("time", "")[:10]] += 1

    return {
        "period": period,
        "label": label,
        "total": len(records),
        "by_source": dict(sorted(by_source.items())),
        "by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))[:5]),
        "trend": dict(sorted(trend.items())),
        "records": records,
    }


def get_block_records(query: str = "", source: str = "all", limit: int = 500, account_id: Optional[str] = None) -> list:
    """Filter persisted block records for the history table."""
    query = (query or "").strip().lower()
    source = source or "all"
    records = []
    for record in reversed(load_block_log(account_id).get("blocks", [])):
        raw_source = record.get("source")
        if source == "private":
            matches_source = raw_source in {"private", "scan"}
        else:
            matches_source = source == "all" or raw_source == source
        if not matches_source:
            continue
        haystack = " ".join(str(record.get(key, "")) for key in ("user_id", "name", "reason", "source"))
        if query and query not in haystack.lower():
            continue
        records.append(dict(record))
        if len(records) >= max(1, int(limit)):
            break
    return records


def export_block_records(path: str, query: str = "", source: str = "all", fmt: str = "json", account_id: Optional[str] = None) -> int:
    """Export filtered block records as JSON or CSV; return row count."""
    records = get_block_records(query, source, account_id=account_id)
    output = Path(path).expanduser()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fmt.lower() == "csv" or output.suffix.lower() == ".csv":
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["time", "source", "user_id", "name", "reason"])
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in writer.fieldnames} for row in records)
    else:
        output.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    output.chmod(0o600)
    return len(records)


def list_entries(list_type: str, query: str = "", account_id: Optional[str] = None) -> list:
    if list_type not in {"whitelist", "blacklist"}:
        raise ValueError("list_type 必須是 whitelist 或 blacklist")
    query = (query or "").strip().lower()
    result = []
    for user_id, info in sorted(load_config(account_id).get(list_type, {}).items()):
        row = {
            "user_id": str(user_id),
            "username": info.get("username", ""),
            "added": info.get("added", ""),
            "reason": info.get("reason", ""),
        }
        if query and query not in " ".join(str(value) for value in row.values()).lower():
            continue
        result.append(row)
    return result


def upsert_list_entry(list_type: str, user_id: str, username: str = "", reason: str = "manual", account_id: Optional[str] = None) -> dict:
    if list_type not in {"whitelist", "blacklist"}:
        raise ValueError("list_type 必須是 whitelist 或 blacklist")
    user_id = str(user_id).strip()
    if not user_id or not user_id.lstrip("-").isdigit():
        raise ValueError("使用者 ID 必須是 numeric Telegram user ID")
    cfg = load_config(account_id)
    entries = cfg.setdefault(list_type, {})
    previous = entries.get(user_id, {})
    entries[user_id] = {
        "added": previous.get("added") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "username": (username or previous.get("username", "")).lstrip("@"),
        "reason": reason or previous.get("reason", "manual"),
    }
    save_config(cfg, account_id)
    return list_entries(list_type, user_id, account_id=account_id)[0]


def remove_list_entry(list_type: str, user_id: str, account_id: Optional[str] = None) -> bool:
    if list_type not in {"whitelist", "blacklist"}:
        raise ValueError("list_type 必須是 whitelist 或 blacklist")
    cfg = load_config(account_id)
    entries = cfg.setdefault(list_type, {})
    existed = str(user_id) in entries
    entries.pop(str(user_id), None)
    if existed:
        save_config(cfg, account_id)
    return existed


def export_list_entries(path: str, list_type: str, fmt: str = "", account_id: Optional[str] = None) -> int:
    rows = list_entries(list_type, account_id=account_id)
    output = Path(path).expanduser()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (fmt or output.suffix.lstrip(".")).lower() == "csv":
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["user_id", "username", "added", "reason"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    output.chmod(0o600)
    return len(rows)


def import_list_entries(path: str, list_type: str, replace: bool = False, account_id: Optional[str] = None) -> int:
    if list_type not in {"whitelist", "blacklist"}:
        raise ValueError("list_type 必須是 whitelist 或 blacklist")
    source = Path(path).expanduser()
    if source.suffix.lower() == ".csv":
        with source.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    else:
        data = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            rows = [dict(info, user_id=user_id) for user_id, info in data.items()]
        else:
            rows = data
    cfg = load_config(account_id)
    if replace:
        cfg[list_type] = {}
        save_config(cfg, account_id)
    imported = 0
    for row in rows:
        try:
            upsert_list_entry(list_type, row.get("user_id", ""), row.get("username", ""), row.get("reason", "import"), account_id=account_id)
            imported += 1
        except ValueError:
            continue
    return imported


def merge_managed_groups(groups: list, account_id: Optional[str] = None) -> list:
    """Merge Telegram discovery results without resetting enable switches."""
    cfg = load_config(account_id)
    existing = {str(group.get("id")): dict(group) for group in cfg.get("managed_groups", [])}
    seen = set()
    merged = []
    for group in groups or []:
        group_id = str(group.get("id"))
        if group_id in seen or group_id in {"None", ""}:
            continue
        seen.add(group_id)
        old = existing.get(group_id, {})
        merged.append({**old, **group, "id": group_id, "enabled": old.get("enabled", True)})
    for group_id, old in existing.items():
        if group_id not in seen:
            merged.append(old)
    cfg["managed_groups"] = merged
    save_config(cfg, account_id)
    return merged


def set_managed_group_enabled(group_id: str, enabled: bool, account_id: Optional[str] = None) -> bool:
    cfg = load_config(account_id)
    group_id = str(group_id)
    groups = cfg.setdefault("managed_groups", [])
    for group in groups:
        if str(group.get("id")) == group_id:
            group["enabled"] = bool(enabled)
            save_config(cfg, account_id)
            return True
    groups.append({"id": group_id, "title": group_id, "username": "", "enabled": bool(enabled)})
    save_config(cfg, account_id)
    return True


def is_group_enabled(group_id: str, cfg: dict = None, account_id: Optional[str] = None) -> bool:
    cfg = cfg if cfg is not None else load_config(account_id)
    groups = cfg.get("managed_groups") or []
    if not groups:
        return bool(cfg.get("listen_scan_groups", True))
    for group in groups:
        if str(group.get("id")) == str(group_id):
            return bool(group.get("enabled", True))
    return False


def clear_local_session(remove_credentials: bool = False, account_id: Optional[str] = None) -> None:
    """Delete one account's local Telegram session and identity fields only."""
    store = _resolve_account_store(account_id)
    with _account_session_lease(store=store):
        effective_account_id = account_id or store.account_id
        for path in (store.session_file, Path(f"{store.session_file}-journal")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        cfg = load_config(account_id)
        for key in ("phone", "user_id", "username", "last_scan"):
            cfg.pop(key, None)
        if remove_credentials:
            cfg.pop("api_id", None)
            cfg.pop("api_hash", None)
        save_config(cfg, account_id)
        if effective_account_id:
            root_path = store.data_root
            with _account_registry_transaction(root_path):
                registry = _read_account_registry(root_path)
                for record in registry["accounts"]:
                    if str(record.get("id")) == str(effective_account_id):
                        record.update({"user_id": None, "username": "", "display_name": "", "phone_masked": ""})
                        break
                _write_account_registry(registry, root_path)


def find_tesseract() -> Optional[str]:
    """Find a system or bundled Tesseract executable without exposing secrets."""
    candidates = []
    configured = os.getenv("TELESHIELD_TESSERACT_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundle = Path(bundle_root)
        candidates.extend([
            bundle / "tesseract-runtime" / "bin" / "tesseract",
            bundle / "tesseract-runtime" / "tesseract",
            bundle / "tesseract",
        ])
    system_path = shutil.which("tesseract")
    if system_path:
        candidates.append(Path(system_path))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def get_ocr_status() -> dict:
    path = find_tesseract()
    bundled = bool(path and getattr(sys, "_MEIPASS", None) and str(path).startswith(str(sys._MEIPASS)))
    return {"available": bool(path), "bundled": bundled, "languages": ["chi_sim", "eng"] if path else []}

def is_spam(text: str, cfg: dict = None) -> bool:
    """檢查文字是否包含廣告模式（含自訂模式）"""
    if not text:
        return False
    # 內建模式
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    # 自訂學習模式
    if cfg is not None:
        lp = get_learned_patterns(cfg)
        for p in lp.get("patterns", []):
            try:
                if re.search(p, text, re.IGNORECASE):
                    return True
            except:
                continue
        for kw in lp.get("keywords", []):
            if kw.lower() in text.lower():
                return True
    return False

def is_blacklisted(user_id: int, cfg: dict) -> bool:
    return str(user_id) in cfg.get("blacklist", {})

def is_whitelisted(user_id: int, cfg: dict) -> bool:
    return str(user_id) in cfg.get("whitelist", {})

def log_block(user_id: int, name: str, reason: str, source: str = "private"):
    log = load_block_log()
    log["blocks"].append({
        "user_id": user_id,
        "name": name,
        "reason": reason[:200],
        "source": source,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    # 保留最近 500 筆
    if len(log["blocks"]) > 500:
        log["blocks"] = log["blocks"][-500:]
    save_block_log(log)

def ocr_image(image_path: str) -> str:
    """Extract Chinese/English text when a system or bundled Tesseract exists."""
    try:
        from PIL import Image
        import pytesseract

        tesseract_path = find_tesseract()
        if not tesseract_path:
            return ""
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        config = ""
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            tessdata = Path(bundle_root) / "tesseract-runtime" / "share" / "tessdata"
            if tessdata.is_dir():
                config = f'--tessdata-dir "{tessdata}"'
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng", config=config)
        return text.strip()
    except Exception:
        return ""


async def check_photo(client, msg) -> str:
    if not msg or not msg.photo:
        return ""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp = f.name
        await client.download_media(msg, file=tmp)
        return ocr_image(tmp)
    except Exception:
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

# ──────────── 學習模式 ────────────

async def learn(text: str):
    """CLI wrapper around the GUI-safe learning API."""
    try:
        result = learn_text(text)
    except ValueError as exc:
        print(f"❌ {exc}")
        return
    print(f"✅ 已學習 {len(result['added_keywords'])} 個關鍵詞 + {len(result['added_patterns'])} 個模式")
    if result["added_keywords"]:
        print(f"   關鍵詞: {', '.join(result['added_keywords'])}")
    if result["added_patterns"]:
        print(f"   模式: {', '.join(result['added_patterns'][:5])}")
    print(f"   累計: {result['total_keywords']} 關鍵詞, {result['total_patterns']} 模式")

# ──────────── 白名單/黑名單管理 ────────────

async def manage_list(action: str, list_type: str, user_id_str: str = None):
    """CLI wrapper around the shared whitelist/blacklist API."""
    try:
        if action == "list":
            rows = list_entries(list_type)
            if not rows:
                print(f"📋 {list_type} 名單: 空")
            else:
                print(f"📋 {list_type} 名單 ({len(rows)} 人):")
                for row in rows:
                    tag = f"@{row['username']}" if row["username"] else ""
                    print(f"  • {row['user_id']} {tag} ({row['added'] or '?'})")
            return
        if not user_id_str:
            print("❌ 請提供使用者 ID")
            return
        if action == "add":
            upsert_list_entry(list_type, user_id_str, reason="manual")
            print(f"✅ 已將 {user_id_str} 加入 {list_type} 名單")
        elif action == "remove":
            if remove_list_entry(list_type, user_id_str):
                print(f"✅ 已將 {user_id_str} 從 {list_type} 名單移除")
            else:
                print(f"❌ {user_id_str} 不在 {list_type} 名單中")
        else:
            print(f"❌ 未知操作: {action}")
    except ValueError as exc:
        print(f"❌ {exc}")

# ──────────── 封鎖摘要報告 ────────────

async def report(period: str = "day"):
    """CLI wrapper around the shared structured report API."""
    result = build_report(period)
    if not result["total"]:
        print(f"📊 {result['label']}: 無封鎖記錄")
        return
    print(f"\n📊 封鎖摘要 — {result['label']}")
    print(f"{'─'*40}")
    print(f"   總計封鎖: {result['total']} 人")
    if result["by_source"]:
        print("   來源:")
        for source, count in result["by_source"].items():
            print(f"     • {'私訊' if source == 'private' else '群組'}: {count} 人")
    print("   廣告類型 Top 5:")
    for reason, count in result["by_reason"].items():
        print(f"     • {reason}: {count} 次")
    if period == "week":
        print("\n   每日趨勢:")
        for day, count in result["trend"].items():
            print(f"     {day}: {count} 人")

# ──────────── 首次設定 ────────────

@_session_leased
async def authenticate(
    api_id: str,
    api_hash: str,
    phone: str,
    code_callback,
    password_callback,
    status_callback=None,
    account_id: Optional[str] = None,
):
    """Authenticate one personal Telegram account in its isolated store."""
    with account_context(account_id):
        return await _authenticate(api_id, api_hash, phone, code_callback, password_callback, status_callback)


async def _authenticate(
    api_id: str,
    api_hash: str,
    phone: str,
    code_callback,
    password_callback,
    status_callback=None,
):
    """Authenticate the personal Telegram client without requiring a CLI prompt."""
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    store = _resolve_account_store()
    store.ensure()
    session_paths = [store.session_file, Path(f"{store.session_file}-journal")]
    session_backups = {}
    for path in session_paths:
        if not path.exists():
            continue
        backup = path.with_name(f".{path.name}.backup-{uuid4().hex}")
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        session_backups[path] = backup
    restore_session = True
    client = None
    try:
        previous_cfg = load_config()
        previous_phone = str(previous_cfg.get("phone", "") or "").strip()
        requested_phone = str(phone or "").strip()
        if previous_phone and requested_phone and previous_phone != requested_phone:
            for path in session_paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        client = TelegramClient(str(store.session_file), int(api_id), api_hash)
        await client.connect()
        store.ensure()
        if not await client.is_user_authorized():
            sent_code = await client.send_code_request(phone)
            if status_callback:
                delivery = type(getattr(sent_code, "type", None)).__name__
                delivery = delivery.replace("SentCodeType", "") or "未知方式"
                details = []
                next_type = getattr(sent_code, "next_type", None)
                if next_type is not None:
                    next_delivery = type(next_type).__name__.replace("SentCodeType", "")
                    if next_delivery:
                        details.append(f"下一個可用方式：{next_delivery}")
                timeout = getattr(sent_code, "timeout", None)
                if timeout is not None:
                    details.append(f"等待時間：{timeout} 秒")
                status_callback("；".join([delivery, *details]))
            code_value = await code_callback()
            if not code_value:
                raise ValueError("Telegram 驗證碼不可為空")
            try:
                await client.sign_in(phone=phone, code=code_value)
            except SessionPasswordNeededError:
                password_value = await password_callback()
                if not password_value:
                    raise ValueError("Telegram 兩步驟驗證密碼不可為空")
                await client.sign_in(password=password_value)

        me = await client.get_me()
        cfg = load_config()
        cfg.update({
            "api_id": int(api_id),
            "api_hash": api_hash,
            "phone": phone,
            "user_id": me.id,
            "username": me.username,
        })
        cfg.setdefault("blocked_count", 0)
        cfg.setdefault("kicked_count", 0)
        cfg.setdefault("last_scan", None)
        cfg.setdefault("whitelist", {})
        cfg.setdefault("blacklist", {})
        cfg.setdefault("managed_groups", [])
        cfg.setdefault("learned_patterns", {"keywords": [], "patterns": []})
        cfg.setdefault("scan_settings", DEFAULT_SCAN_SETTINGS.copy())
        cfg.setdefault("listen_scan_groups", True)
        cfg.setdefault("auto_start_protection", False)
        if store.account_id:
            _commit_account_identity_and_config(
                store.account_id,
                me,
                phone,
                cfg,
                root=store.data_root,
            )
        else:
            save_config(cfg)
        restore_session = False
        return me
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            cleanup_failure = None
            if restore_session:
                for path in session_paths:
                    backup = session_backups.get(path)
                    if backup is not None and backup.exists():
                        shutil.copy2(backup, path)
                        path.chmod(0o600)
                    elif path.exists():
                        try:
                            path.unlink()
                        except OSError as exc:
                            try:
                                _write_private_bytes(path, b"")
                            except OSError as scrub_exc:
                                raise RuntimeError("Session 清理失敗，而且無法清空授權資料") from scrub_exc
                            cleanup_failure = exc
            for backup in session_backups.values():
                try:
                    backup.unlink()
                except FileNotFoundError:
                    pass
            store.ensure()
            if cleanup_failure is not None:
                raise RuntimeError("Session 清理失敗；授權資料已清空，請手動刪除空檔") from cleanup_failure


@_session_leased
async def discover_managed_groups(account_id: Optional[str] = None) -> list:
    """Fetch groups for one account where it has moderation privileges."""
    with account_context(account_id):
        return await _discover_managed_groups()


async def _discover_managed_groups() -> list:
    """Fetch groups where the current account has moderation privileges."""
    from telethon import TelegramClient
    from telethon.tl.types import Chat, Channel

    store = _resolve_account_store()
    store.ensure()
    cfg = load_config()
    if not cfg.get("api_id"):
        raise RuntimeError("尚未登入 Telegram")
    client = TelegramClient(str(store.session_file), cfg["api_id"], cfg["api_hash"])
    connected = False
    try:
        await client.connect()
        connected = True
        store.ensure()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram Session 已失效，請先重新登入")
        me = await client.get_me()
        groups = []
        for dialog in await client.get_dialogs(limit=100):
            entity = dialog.entity
            if not isinstance(entity, (Chat, Channel)) or getattr(entity, "broadcast", False):
                continue
            try:
                permission = await client.get_permissions(entity, me.id)
            except Exception:
                continue
            if not permission or not (permission.is_admin or permission.is_creator):
                continue
            groups.append({
                "id": str(entity.id),
                "title": getattr(entity, "title", "未命名群組"),
                "username": getattr(entity, "username", "") or "",
                "is_creator": bool(getattr(permission, "is_creator", False)),
                "is_admin": bool(getattr(permission, "is_admin", False)),
            })
        return merge_managed_groups(groups)
    finally:
        try:
            if connected:
                await client.disconnect()
        finally:
            store.ensure()


@_session_leased
async def logout_account(remove_credentials: bool = False, account_id: Optional[str] = None) -> bool:
    """Log out one Telegram session and then clear only that account's identity data."""
    with account_context(account_id):
        return await _logout_account(remove_credentials)


async def _logout_account(remove_credentials: bool = False) -> bool:
    """Log out the Telegram session and then clear local identity data."""
    from telethon import TelegramClient

    store = _resolve_account_store()
    store.ensure()
    cfg = load_config()
    if not cfg.get("api_id"):
        clear_local_session(remove_credentials=remove_credentials)
        return False
    client = TelegramClient(str(store.session_file), cfg["api_id"], cfg["api_hash"])
    connected = False
    logged_out = False
    try:
        await client.connect()
        connected = True
        store.ensure()
        if await client.is_user_authorized():
            await client.log_out()
            logged_out = True
    finally:
        try:
            if connected:
                await client.disconnect()
        finally:
            store.ensure()
    clear_local_session(remove_credentials=remove_credentials)
    return logged_out


async def setup(api_id: str = None, api_hash: str = None, phone: str = None, code: str = None):

    print("\n═══════════════════════════════")
    print("  TeleShield - 設定")
    print("═══════════════════════════════\n")

    if not api_id:
        api_id = input("API ID (從 my.telegram.org/apps 取得): ").strip()
    else:
        print(f"API ID: {api_id}")
    if not api_hash:
        api_hash = input("API Hash: ").strip()
    else:
        print(f"API Hash: {api_hash}")
    if not phone:
        phone = input("手機號碼 (含國碼，如 +852****5931): ").strip()
    else:
        print(f"手機隱藏")

    try:
        async def code_callback():
            return code or await asyncio.to_thread(input, "請輸入驗證碼: ")

        async def password_callback():
            return await asyncio.to_thread(input, "請輸入兩步驟驗證密碼: ")

        me = await authenticate(api_id, api_hash, phone, code_callback, password_callback)
        print(f"\n✅ 登入成功！")
        print(f"   帳號: {me.first_name} (@{me.username or '無'})")
        print(f"   ID: {me.id}")
        print("✅ 設定已儲存")
        return True
    except Exception as e:
        print(f"\n❌ 登入失敗: {e}")
        return False

# ──────────── 歷史訊息掃描核心 ────────────

@_session_leased
async def scan_history(
    scope: str = "private",
    dry_run: bool = False,
    progress_callback=None,
    cancel_event=None,
    account_id: Optional[str] = None,
):
    """Scan recent history using one account's isolated client and storage."""
    with account_context(account_id):
        return await _scan_history(scope, dry_run, progress_callback, cancel_event)


async def _scan_history(
    scope: str = "private",
    dry_run: bool = False,
    progress_callback=None,
    cancel_event=None,
):
    """Scan recent private/group history and optionally apply moderation.

    This API is UI-friendly: it never prompts on stdin, reports progress through
    ``progress_callback``, and checks ``cancel_event`` between Telegram calls.
    The existing CLI commands remain available below for backwards compatibility.
    """
    from telethon import TelegramClient
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.functions.contacts import BlockRequest, GetContactsRequest
    from telethon.tl.types import Chat, ChatBannedRights, Channel, User

    if scope not in {"private", "group"}:
        raise ValueError("scope 必須是 private 或 group")

    store = _resolve_account_store()
    store.ensure()
    cfg = load_config()
    if not cfg.get("api_id"):
        raise RuntimeError("尚未登入 Telegram")

    result = {
        "scope": scope,
        "dry_run": dry_run,
        "dialogs_seen": 0,
        "dialogs_scanned": 0,
        "groups_found": 0,
        "messages_scanned": 0,
        "matched": 0,
        "acted": 0,
        "errors": [],
        "findings": [],
        "cancelled": False,
    }
    now = datetime.now(timezone.utc)
    scan_settings = get_scan_settings(cfg)

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    def progress(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    def add_error(message: str) -> None:
        result["errors"].append(message)
        progress(f"⚠️ {message}")

    client = TelegramClient(str(store.session_file), cfg["api_id"], cfg["api_hash"])
    connected = False
    try:
        progress("正在連線 Telegram…")
        await client.connect()
        connected = True
        store.ensure()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram Session 已失效，請先重新登入")
        if cancelled():
            result["cancelled"] = True
            return result

        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {contact.id for contact in contacts}

        if scope == "private":
            dialogs = await client.get_dialogs(limit=scan_settings["private_dialog_limit"])
            total = len(dialogs)
            for index, dialog in enumerate(dialogs, 1):
                result["dialogs_seen"] += 1
                if cancelled():
                    result["cancelled"] = True
                    break
                entity = dialog.entity
                if (
                    not isinstance(entity, User)
                    or entity.is_self
                    or entity.bot
                    or entity.id in contact_ids
                    or is_whitelisted(entity.id, cfg)
                ):
                    continue
                result["dialogs_scanned"] += 1
                progress(f"掃描私訊 {index}/{total}…")
                try:
                    messages = await client.get_messages(entity, limit=scan_settings["private_message_limit"])
                except Exception as exc:
                    add_error(f"私訊讀取失敗（{entity.id}）：{exc}")
                    continue
                result["messages_scanned"] += len(messages)

                for message in messages:
                    if cancelled():
                        result["cancelled"] = True
                        break
                    if not message:
                        continue
                    if message.date and message.date < now - timedelta(days=scan_settings["private_days"]):
                        continue
                    reason = message.text or ""
                    if not is_spam(reason, cfg) and message.photo:
                        ocr_text = await check_photo(client, message)
                        if ocr_text and is_spam(ocr_text, cfg):
                            reason = f"[OCR] {ocr_text[:100]}"
                    if not reason or not is_spam(reason, cfg):
                        continue

                    result["matched"] += 1
                    name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                    finding = {
                        "user_id": entity.id,
                        "name": name or str(entity.id),
                        "reason": reason[:120],
                    }
                    result["findings"].append(finding)
                    progress(f"⚠️ 發現私訊廣告：{finding['name']}")
                    if dry_run:
                        break
                    try:
                        await client(BlockRequest(id=entity.id))
                        result["acted"] += 1
                        log_block(entity.id, finding["name"], reason, "scan")
                        progress(f"✅ 已封鎖：{finding['name']}")
                    except Exception as exc:
                        add_error(f"封鎖失敗（{entity.id}）：{exc}")
                    break
        else:
            me = await client.get_me()
            dialogs = await client.get_dialogs(limit=scan_settings["group_dialog_limit"])
            groups = []
            for dialog in dialogs:
                result["dialogs_seen"] += 1
                entity = dialog.entity
                if not isinstance(entity, (Chat, Channel)) or getattr(entity, "broadcast", False):
                    continue
                if not is_group_enabled(entity.id, cfg):
                    continue
                try:
                    permissions = await client.get_permissions(entity, me.id)
                    if permissions and permissions.is_admin:
                        groups.append(dialog)
                except Exception as exc:
                    add_error(f"群組權限讀取失敗（{getattr(entity, 'title', entity.id)}）：{exc}")
            result["groups_found"] = len(groups)
            progress(f"找到 {len(groups)} 個可管理群組")

            handled = set()
            for group_index, dialog in enumerate(groups, 1):
                if cancelled():
                    result["cancelled"] = True
                    break
                entity = dialog.entity
                title = getattr(entity, "title", "未知群組")
                progress(f"掃描群組 {group_index}/{len(groups)}：{title}")
                try:
                    messages = await client.get_messages(entity, limit=scan_settings["group_message_limit"])
                except Exception as exc:
                    add_error(f"群組讀取失敗（{title}）：{exc}")
                    continue
                result["messages_scanned"] += len(messages)

                for message in messages:
                    if cancelled():
                        result["cancelled"] = True
                        break
                    if (
                        not message
                        or not message.sender_id
                        or message.sender_id == me.id
                        or message.sender_id in contact_ids
                        or is_whitelisted(message.sender_id, cfg)
                    ):
                        continue
                    if message.date and message.date < now - timedelta(days=scan_settings["group_days"]):
                        continue

                    reason = message.text or ""
                    if not is_spam(reason, cfg) and message.photo:
                        ocr_text = await check_photo(client, message)
                        if ocr_text and is_spam(ocr_text, cfg):
                            reason = f"[OCR] {ocr_text[:80]}"
                    if not reason or not is_spam(reason, cfg):
                        continue

                    result["matched"] += 1
                    try:
                        sender = await client.get_entity(message.sender_id)
                        name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                    except Exception:
                        name = str(message.sender_id)
                    finding = {
                        "user_id": message.sender_id,
                        "name": name or str(message.sender_id),
                        "group": title,
                        "reason": reason[:100],
                    }
                    result["findings"].append(finding)
                    progress(f"⚠️ 發現群組廣告：{title}／{finding['name']}")
                    action_key = (entity.id, message.sender_id)
                    if dry_run or action_key in handled:
                        continue
                    handled.add(action_key)
                    try:
                        rights = ChatBannedRights(until_date=None, view_messages=True)
                        await client(EditBannedRequest(entity, message.sender_id, rights))
                        result["acted"] += 1
                        log_block(message.sender_id, finding["name"], reason, "group")
                        progress(f"✅ 已踢除：{finding['name']}（{title}）")
                    except Exception as exc:
                        add_error(f"踢除失敗（{message.sender_id}／{title}）：{exc}")

        if not dry_run:
            if scope == "private":
                cfg["blocked_count"] = cfg.get("blocked_count", 0) + result["acted"]
            else:
                cfg["kicked_count"] = cfg.get("kicked_count", 0) + result["acted"]
            cfg["last_scan"] = now.isoformat()
        else:
            cfg["last_preview"] = now.isoformat()
        save_config(cfg)
        return result
    finally:
        try:
            if connected:
                await client.disconnect()
        finally:
            store.ensure()


# ──────────── 掃描私訊封鎖 ────────────

@_session_leased
async def scan_and_block(dry_run: bool = False):
    from telethon import TelegramClient
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.types import User, Message, InputPhoneContact
    from telethon.tl.functions.contacts import GetContactsRequest

    store = _resolve_account_store()
    store.ensure()
    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定，請先執行 --setup")
        return

    print(f"{'🧪 試運行' if dry_run else '🔍 掃描模式'}")
    print(f"{'─'*40}")

    client = TelegramClient(str(store.session_file), cfg["api_id"], cfg["api_hash"])
    try:
        await client.start(phone=cfg["phone"])
        store.ensure()

        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {c.id for c in contacts}
        print(f"📇 聯絡人: {len(contact_ids)} 位")

        now = datetime.now(timezone.utc)
        scan_settings = get_scan_settings(cfg)
        dialogs = await client.get_dialogs(limit=scan_settings["private_dialog_limit"])

        blocked = 0
        skipped = 0

        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, User) or entity.is_self or entity.id in contact_ids or entity.bot:
                continue

            try:
                msgs = await client.get_messages(entity, limit=scan_settings["private_message_limit"])
            except:
                continue

            spam_found = False
            spam_text = ""
            for msg in msgs:
                if not msg:
                    continue
                if msg.date and msg.date < now - timedelta(days=scan_settings["private_days"]):
                    continue
                msg_text = msg.text or ""
                if is_spam(msg_text, cfg):
                    spam_found = True
                    spam_text = msg_text[:120]
                    break
                if msg.photo:
                    ocr_text = await check_photo(client, msg)
                    if ocr_text and is_spam(ocr_text, cfg):
                        spam_found = True
                        spam_text = f"[OCR] {ocr_text[:100]}"
                        break

            if not spam_found:
                continue

            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            uname = f"@{entity.username}" if entity.username else ""
            print(f"\n  ⚠️  廣告: {name} {uname}")
            print(f"      {spam_text[:120]}")

            if dry_run:
                skipped += 1
                continue

            try:
                await client(BlockRequest(id=entity.id))
                blocked += 1
                log_block(entity.id, name, spam_text, "scan")
                print(f"      ✅ 封鎖")
            except Exception as e:
                print(f"      ❌ 失敗: {e}")

        print(f"\n{'─'*40}")
        print(f"結果: 已處理 {blocked+skipped}")
        if not dry_run and blocked > 0:
            cfg["blocked_count"] = cfg.get("blocked_count", 0) + blocked
        cfg["last_scan"] = now.isoformat()
        save_config(cfg)
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
    finally:
        try:
            await client.disconnect()
        finally:
            store.ensure()

# ──────────── 掃描群組踢除 ────────────

@_session_leased
async def scan_groups(dry_run: bool = False):
    """掃描群組訊息，踢除發廣告的成員"""
    from telethon import TelegramClient
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights, User, Chat, Channel
    from telethon.tl.functions.contacts import GetContactsRequest
    from telethon.errors import UserAdminInvalidError

    store = _resolve_account_store()
    store.ensure()
    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定")
        return

    print(f"{'🧪 試運行' if dry_run else '👥 群組掃描模式'}")
    print(f"{'─'*40}")

    client = TelegramClient(str(store.session_file), cfg["api_id"], cfg["api_hash"])
    try:
        await client.start(phone=cfg["phone"])
        store.ensure()
        me = await client.get_me()
        now = datetime.now(timezone.utc)
        scan_settings = get_scan_settings(cfg)

        # 白名單聯絡人
        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {c.id for c in contacts}

        dialogs = await client.get_dialogs(limit=scan_settings["group_dialog_limit"])
        groups = []
        for d in dialogs:
            if isinstance(d.entity, (Chat, Channel)) and not d.entity.broadcast:
                if not is_group_enabled(d.entity.id, cfg):
                    continue
                # 檢查是否為管理員
                try:
                    participant = await client.get_permissions(d.entity, me.id)
                    if participant and participant.is_admin:
                        groups.append(d)
                except:
                    pass

        if not groups:
            print("⚠️  沒有可管理的群組（需要是管理員）")
            return

        print(f"👥 管理中的群組: {len(groups)}")
        kicked = 0
        total_scanned = 0

        for dialog in groups:
            entity = dialog.entity
            title = getattr(entity, "title", "未知群組")
            try:
                msgs = await client.get_messages(entity, limit=scan_settings["group_message_limit"])
            except:
                continue

            for msg in msgs:
                if not msg or not msg.sender_id:
                    continue
                if msg.sender_id == me.id:
                    continue
                if msg.sender_id in contact_ids:
                    continue
                if is_whitelisted(msg.sender_id, cfg):
                    continue
                if msg.date and msg.date < now - timedelta(days=scan_settings["group_days"]):
                    continue

                # 檢測廣告
                msg_text = msg.text or ""
                spam_reason = ""

                if is_spam(msg_text, cfg):
                    spam_reason = msg_text[:100]
                elif msg.photo:
                    ocr_text = await check_photo(client, msg)
                    if ocr_text and is_spam(ocr_text, cfg):
                        spam_reason = f"[OCR] {ocr_text[:80]}"

                if not spam_reason:
                    continue

                total_scanned += 1
                try:
                    sender = await client.get_entity(msg.sender_id)
                    sname = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                except:
                    sname = str(msg.sender_id)

                print(f"\n  ⚠️  [{title}] {sname}")
                print(f"     {spam_reason[:100]}")

                if dry_run:
                    continue

                # 踢除 (ban + kick)
                try:
                    rights = ChatBannedRights(
                        until_date=None,
                        view_messages=True
                    )
                    await client(EditBannedRequest(entity, msg.sender_id, rights))
                    kicked += 1
                    log_block(msg.sender_id, sname, spam_reason, "group")
                    print(f"     ✅ 已踢除")
                    await asyncio.sleep(1)  # 避免 rate limit
                except UserAdminInvalidError:
                    print(f"     ⚠️ 無法踢除（權限不足）")
                except Exception as e:
                    print(f"     ❌ 踢除失敗: {e}")

        print(f"\n{'─'*40}")
        print(f"結果: 掃描 {total_scanned} 條, {'已踢除' if not dry_run else '試運行'}: {kicked if not dry_run else total_scanned}")
        if not dry_run and kicked > 0:
            cfg["kicked_count"] = cfg.get("kicked_count", 0) + kicked
        save_config(cfg)
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
    finally:
        try:
            await client.disconnect()
        finally:
            store.ensure()

# ──────────── 即時監聽（私訊+群組） ────────────

async def listen(
    stop_event: Optional[asyncio.Event] = None,
    ready_callback=None,
    account_id: Optional[str] = None,
) -> bool:
    """Run realtime protection for exactly one account."""
    with account_context(account_id) as store:
        with _account_session_lease(store=store):
            return await _listen(stop_event, ready_callback)


async def _listen(stop_event: Optional[asyncio.Event] = None, ready_callback=None) -> bool:
    from telethon import TelegramClient
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import User, Message, Chat, Channel, ChatBannedRights
    from telethon.tl.functions.contacts import GetContactsRequest

    store = _resolve_account_store()
    store.ensure()
    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定")
        return False

    print("👂 TeleShield 即時監聽啟動中...")
    print("    ✅ 私訊廣告 → 自動封鎖")
    print("    👥 群組廣告 → 自動踢除（管理員身份）")
    print("    📸 OCR 支援 → 純圖片廣告也辨識")
    print("    按 Ctrl+C 停止\n")

    client = TelegramClient(str(store.session_file), cfg["api_id"], cfg["api_hash"])

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        msg = event.message
        if not msg or not msg.sender_id:
            return

        # Reload settings so desktop and Bot controls take effect without
        # restarting the long-running Telethon client.
        cfg = load_config()

        sender_id = msg.sender_id
        chat = await event.get_chat()
        sender = await event.get_sender()
        now = datetime.now(timezone.utc)

        # 跳過自己
        if hasattr(sender, 'is_self') and sender.is_self:
            return

        # 檢查黑名單（無論在哪）
        if is_blacklisted(sender_id, cfg):
            try:
                if isinstance(chat, (Chat, Channel)):
                    rights = ChatBannedRights(until_date=None, view_messages=True)
                    await client(EditBannedRequest(chat, sender_id, rights))
                else:
                    await client(BlockRequest(id=sender_id))
            except:
                pass
            return

        if is_whitelisted(sender_id, cfg):
            return

        # 私訊處理
        if isinstance(chat, User):
            sender = chat
            if sender_id == cfg.get("user_id"):
                return
            if sender.bot:
                return

            # 檢查聯絡人
            try:
                contacts = (await client(GetContactsRequest(hash=0))).users
                contact_ids = {c.id for c in contacts}
                if sender_id in contact_ids:
                    return
            except:
                pass

            # 檢測
            spam_text = msg.text or ""
            is_spam_by_text = is_spam(spam_text, cfg)
            ocr_found_spam = False
            if not is_spam_by_text and msg.photo:
                ocr_text = await check_photo(client, msg)
                if ocr_text and is_spam(ocr_text, cfg):
                    ocr_found_spam = True
                    spam_text = ocr_text[:100]

            if not is_spam_by_text and not ocr_found_spam:
                return

            name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
            uname = f"@{sender.username}" if sender.username else ""
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            icon = "📸" if ocr_found_spam else ""
            print(f"\n[{ts}] {icon}⚠️  私訊廣告: {name} {uname}")
            print(f"    {spam_text[:100]}")

            try:
                await client(BlockRequest(id=sender_id))
                cfg["blocked_count"] = cfg.get("blocked_count", 0) + 1
                save_config(cfg)
                log_block(sender_id, name, spam_text, "private")
                print(f"     ✅ 封鎖（累計 {cfg['blocked_count']}）")
            except Exception as e:
                print(f"     ❌ 封鎖失敗: {e}")
            return

        # 群組處理
        if isinstance(chat, (Chat, Channel)) and not chat.broadcast:
            if not is_group_enabled(chat.id, cfg):
                return
            # 檢查是否為管理員
            try:
                me = await client.get_me()
                perm = await client.get_permissions(chat, me.id)
                if not perm or not perm.is_admin:
                    return
            except:
                return

            # 跳過管理員
            try:
                s_perm = await client.get_permissions(chat, sender_id)
                if s_perm and (s_perm.is_admin or s_perm.is_creator):
                    return
            except:
                pass

            # 檢測
            msg_text = msg.text or ""
            spam_reason = ""
            if is_spam(msg_text, cfg):
                spam_reason = msg_text[:100]
            elif msg.photo:
                ocr_text = await check_photo(client, msg)
                if ocr_text and is_spam(ocr_text, cfg):
                    spam_reason = f"[OCR] {ocr_text[:80]}"

            if not spam_reason:
                return

            sname = f"{sender.first_name or ''} {sender.last_name or ''}".strip() if hasattr(sender, 'first_name') else str(sender_id)
            title = getattr(chat, "title", "群組")
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[{ts}] 👥 群組廣告 [{title}]: {sname}")
            print(f"    {spam_reason[:100]}")

            try:
                rights = ChatBannedRights(until_date=None, view_messages=True)
                await client(EditBannedRequest(chat, sender_id, rights))
                cfg["kicked_count"] = cfg.get("kicked_count", 0) + 1
                save_config(cfg)
                log_block(sender_id, sname, spam_reason, "group")
                print(f"     ✅ 已踢除（累計 {cfg['kicked_count']}）")
            except Exception as e:
                print(f"     ❌ 踢除失敗: {e}")

    try:
        # Do not call ``start(phone=...)`` here: in a windowless packaged app
        # Telethon could fall back to stdin prompts if the Session expires.
        await client.connect()
        store.ensure()
        if not await client.is_user_authorized():
            print("❌ Telegram Session 已失效，請從桌面 App 重新登入")
            return False
        if ready_callback:
            ready_callback()
        print(f"✅ TeleShield 已上線 — 監聽中...")
        run_task = asyncio.create_task(client.run_until_disconnected())
        if stop_event is None:
            await run_task
        else:
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {run_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if run_task in done:
                # Retrieve listener exceptions so asyncio does not emit an
                # unhandled "Task exception was never retrieved" warning.
                await run_task
            elif stop_task in done:
                await client.disconnect()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except KeyboardInterrupt:
        print("\n\n👋 已停止")
        return True
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        return False
    finally:
        try:
            await client.disconnect()
        finally:
            store.ensure()

    return True

# ──────────── 主程式 ────────────

async def main():
    try:
        ensure_account_registry()
    except Exception as exc:
        print(f"❌ 帳號資料初始化／遷移失敗：{exc}")
        return
    if len(sys.argv) < 2:
        print("TeleShield — Telegram 廣告封鎖工具")
        print(f"{'─'*35}")
        print("  --setup                   首次設定")
        print("  --scan                    掃描並封鎖私訊")
        print("  --dry-run                 試掃描")
        print("  --listen                  即時監聽（後台常駐）")
        print("  --group-scan              掃描群組並踢除廣告")
        print("  --status                  查看狀態")
        print("  --report [day|week]       封鎖摘要報告")
        print("  --learn <文字>            手動標記學習新模式")
        print("  --whitelist add|remove|list [id]")
        print("  --blacklist add|remove|list [id]")
        return

    cmd = sys.argv[1]

    if cmd == "--setup":
        await setup(
            sys.argv[2] if len(sys.argv) > 2 else None,
            sys.argv[3] if len(sys.argv) > 3 else None,
            sys.argv[4] if len(sys.argv) > 4 else None,
            sys.argv[5] if len(sys.argv) > 5 else None,
        )
    elif cmd == "--scan":
        await scan_and_block(dry_run=False)
    elif cmd == "--dry-run":
        await scan_and_block(dry_run=True)
    elif cmd == "--group-scan":
        await scan_groups(dry_run="--dry" in sys.argv or "dry" in sys.argv)
    elif cmd == "--listen":
        await listen()
    elif cmd == "--status":
        cfg = load_config()
        if not cfg:
            print("❌ 尚未設定")
            return
        log = load_block_log()
        recent = len([b for b in log.get("blocks", []) if datetime.fromisoformat(b["time"]) > datetime.now(timezone.utc) - timedelta(days=1)])
        print("📊 TeleShield 狀態")
        print(f"{'─'*30}")
        print(f"  帳號: {cfg.get('username','?')} (ID: {cfg.get('user_id','?')})")
        print(f"  累計封鎖私訊: {cfg.get('blocked_count',0)} 人")
        print(f"  累計踢除群組: {cfg.get('kicked_count',0)} 人")
        print(f"  今日封鎖: {recent} 人")
        print(f"  白名單: {len(cfg.get('whitelist',{}))} 人")
        print(f"  黑名單: {len(cfg.get('blacklist',{}))} 人")
        print(f"  學習模式: {len(cfg.get('learned_patterns',{}).get('keywords',[]))} 關鍵詞")
        print(f"  最後掃描: {cfg.get('last_scan','從未')}")
    elif cmd == "--report":
        period = sys.argv[2] if len(sys.argv) > 2 else "day"
        await report(period)
    elif cmd == "--learn":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not text:
            print("❌ 請提供廣告文字，例如: --learn 加我微信 xxx 投資穩賺")
            return
        await learn(text)
    elif cmd in ("--whitelist", "--blacklist"):
        list_type = cmd.replace("--", "")
        action = sys.argv[2] if len(sys.argv) > 2 else "list"
        user_id = sys.argv[3] if len(sys.argv) > 3 else None
        await manage_list(action, list_type, user_id)
    else:
        print(f"❌ 未知指令: {cmd}")
        print("執行不加參數查看全部指令")

if __name__ == "__main__":
    asyncio.run(main())
