"""Headless JSON-RPC service for the native macOS SwiftUI shell.

The service deliberately keeps the existing Telegram/domain implementation in
``teleshield.py`` and owns only process lifecycle, IPC, and background jobs.
It never imports PySide6, so the frozen helper can be packaged without Qt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import io
import json
import re
from queue import Empty, Queue
import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable, TextIO
from uuid import uuid4


def _load_core() -> Any:
    import teleshield

    return teleshield


def _load_platform() -> Any:
    from desktop_platform import is_start_on_login_enabled, set_start_on_login

    return SimpleNamespace(
        is_start_on_login_enabled=is_start_on_login_enabled,
        set_start_on_login=set_start_on_login,
    )


class InvalidRequestError(ValueError):
    """The JSON-RPC request is malformed."""


class UnknownMethodError(ValueError):
    """The requested service method does not exist."""


_REDACTION_PATTERN = re.compile(
    r"(?i)(\b(?:api[_ -]?hash|password|passwd|secret|token|session(?:[_ -]?string)?|"
    r"phone(?:[_ -]?number)?|verification[_ -]?code)\b\s*[:=]\s*)"
    r"([\"']?)([^\"'\s,;}\]]+)"
)


def _redact_text(value: Any, extra_values: Any = ()) -> str:
    """Remove credential-like values before text reaches RPC or UI events."""
    text = str(value)
    secrets = sorted(
        {
            str(item)
            for item in extra_values
            if item is not None and len(str(item)) >= 3
        },
        key=len,
        reverse=True,
    )
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return _REDACTION_PATTERN.sub(r"\1[REDACTED]", text)


def _redact_payload(value: Any, extra_values: Any = ()) -> Any:
    if isinstance(value, dict):
        return {key: _redact_payload(item, extra_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, extra_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_payload(item, extra_values) for item in value)
    if isinstance(value, str):
        return _redact_text(value, extra_values)
    return value


@dataclass
class ListenerRuntime:
    account_id: str | None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    state: str = "starting"
    ready: bool = False
    error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())


@dataclass
class AuthRuntime:
    flow_id: str
    account_id: str | None
    code_queue: Queue[str] = field(default_factory=Queue)
    password_queue: Queue[str] = field(default_factory=Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    state: str = "starting"
    sensitive_values: list[str] = field(default_factory=list)


class _EventLogWriter(io.TextIOBase):
    """Turn legacy core ``print`` calls into structured service events."""

    def __init__(self, service: "CoreService", level: str, extra_values: Any = ()):
        super().__init__()
        self.service = service
        self.level = level
        self.extra_values = extra_values
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def _emit(self, message: str) -> None:
        message = message.strip()
        if message:
            self.service._emit_event(
                {"event": "log", "level": self.level, "message": message},
                extra_values=self.extra_values,
            )


class CoreService:
    """Expose the reusable TeleShield core through a line-delimited protocol."""

    def __init__(
        self,
        core: Any | None = None,
        emit_event: Callable[[dict[str, Any]], None] | None = None,
        platform: Any | None = None,
    ):
        self.core = core or _load_core()
        self.platform = platform or _load_platform()
        self._event_sink = emit_event
        self._listeners: dict[str, ListenerRuntime] = {}
        self._auth_flows: dict[str, AuthRuntime] = {}
        self._jobs: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._sensitive_values: list[str] = []
        self._scan_jobs: dict[str, str] = {}
        self._lock = threading.RLock()
        self._shutdown_requested = False

    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not isinstance(method, str) or not method:
            raise InvalidRequestError("method 必須是非空字串")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise InvalidRequestError("params 必須是 JSON object")

        handlers = {
            "get_status": self._get_status,
            "list_accounts": self._list_accounts,
            "create_account": self._create_account,
            "remove_account": self._remove_account,
            "get_account_details": self._get_account_details,
            "select_account": self._select_account,
            "set_auto_start": self._set_auto_start,
            "get_startup_status": self._get_startup_status,
            "set_startup": self._set_startup,
            "start_auth": self._start_auth,
            "submit_auth_code": self._submit_auth_code,
            "submit_auth_password": self._submit_auth_password,
            "cancel_auth": self._cancel_auth,
            "get_ocr_status": self._get_ocr_status,
            "start_protection": self._start_protection,
            "stop_protection": self._stop_protection,
            "stop_all": self._stop_all,
            "list_entries": self._list_entries,
            "upsert_list_entry": self._upsert_list_entry,
            "remove_list_entry": self._remove_list_entry,
            "import_list": self._import_list,
            "export_list": self._export_list,
            "get_learned_patterns": self._get_learned_patterns,
            "learn_text": self._learn_text,
            "remove_learned_pattern": self._remove_learned_pattern,
            "get_block_records": self._get_block_records,
            "export_blocks": self._export_blocks,
            "build_report": self._build_report,
            "discover_groups": self._discover_groups,
            "set_group_enabled": self._set_group_enabled,
            "logout": self._logout,
            "clear_session": self._clear_session,
            "get_scan_settings": self._get_scan_settings,
            "update_scan_settings": self._update_scan_settings,
            "start_scan": self._start_scan,
            "cancel_scan": self._cancel_scan,
            "shutdown": self._shutdown,
        }
        handler = handlers.get(method)
        if handler is None:
            raise UnknownMethodError(f"未知方法：{method}")
        return handler(params)

    def handle_request(self, request: Any) -> dict[str, Any]:
        request_id = request.get("id") if isinstance(request, dict) else None
        request_params = request.get("params") if isinstance(request, dict) else {}
        extra_values = request_params.values() if isinstance(request_params, dict) else ()
        try:
            if not isinstance(request, dict):
                raise InvalidRequestError("request 必須是 JSON object")
            method = request.get("method")
            params = request.get("params") or {}
            result = self.dispatch(method, params)
            return {"id": request_id, "ok": True, "result": result}
        except Exception as exc:  # errors are returned, never leaked as tracebacks
            return {
                "id": request_id,
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": _redact_text(str(exc), extra_values),
                },
            }

    def run(self, reader: TextIO, writer: TextIO) -> None:
        """Serve one JSON object per line until EOF or ``shutdown``."""

        write_lock = threading.Lock()

        def send(payload: dict[str, Any]) -> None:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with write_lock:
                writer.write(line + "\n")
                writer.flush()

        if self._event_sink is None:
            self._event_sink = send

        previous_stdout, previous_stderr = sys.stdout, sys.stderr
        sys.stdout = _EventLogWriter(self, "stdout", self._sensitive_values)
        sys.stderr = _EventLogWriter(self, "stderr", self._sensitive_values)
        try:
            for raw_line in reader:
                if not raw_line.strip():
                    continue
                try:
                    request = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    send(
                        {
                            "id": None,
                            "ok": False,
                            "error": {
                                "type": "InvalidRequestError",
                                "message": f"JSON 格式錯誤：{exc.msg}",
                            },
                        }
                    )
                    continue
                send(self.handle_request(request))
                if self._shutdown_requested:
                    break
        finally:
            self.close()
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = previous_stdout, previous_stderr

    def close(self) -> None:
        with self._lock:
            self._shutdown_requested = True
            auth_flows = list(self._auth_flows.values())
            jobs = list(self._jobs.values())
        for runtime in auth_flows:
            runtime.cancel_event.set()
        for _thread, cancel_event in jobs:
            cancel_event.set()
        self._stop_all({})

        current = threading.current_thread()
        remaining: list[str] = []
        for runtime in auth_flows:
            thread = runtime.thread
            if thread and thread is not current and thread.is_alive():
                thread.join(timeout=5)
            if thread and thread.is_alive():
                remaining.append(f"auth:{runtime.flow_id}")
        for job_id, (thread, _cancel_event) in list(self._jobs.items()):
            if thread is not current and thread.is_alive():
                thread.join(timeout=5)
            if thread.is_alive():
                remaining.append(f"job:{job_id}")
        with self._lock:
            for flow_id, runtime in list(self._auth_flows.items()):
                if not runtime.thread or not runtime.thread.is_alive():
                    self._auth_flows.pop(flow_id, None)
            for job_id, (thread, _cancel_event) in list(self._jobs.items()):
                if not thread.is_alive():
                    self._jobs.pop(job_id, None)
        if remaining:
            self._emit_event({"event": "shutdown_incomplete", "workers": remaining})

    def _emit_event(self, event: dict[str, Any], extra_values: Any = ()) -> None:
        sink = self._event_sink
        if sink is not None:
            sink(_redact_payload(event, extra_values))

    def _resolve_account_id(self, params: dict[str, Any]) -> str | None:
        value = params.get("account_id")
        if value not in (None, ""):
            return str(value)
        return self.core.get_active_account_id()

    @staticmethod
    def _listener_key(account_id: str | None) -> str:
        return account_id or "__legacy__"

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "0", "false", "no", "off", "null", "none"}:
                return False
            if normalized in {"1", "true", "yes", "on"}:
                return True
        return bool(value)

    def _runtime(self, account_id: str | None) -> ListenerRuntime | None:
        return self._listeners.get(self._listener_key(account_id))

    def _safe_account_record(self, record: dict[str, Any]) -> dict[str, Any]:
        allowed = (
            "id",
            "user_id",
            "username",
            "display_name",
            "phone_masked",
            "created_at",
            "last_used_at",
            "auto_start_protection",
        )
        return {key: record.get(key) for key in allowed if key in record}

    def _recent_block_count(self, account_id: str | None, cfg: dict[str, Any]) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        recent = 0
        try:
            records = self.core.load_block_log(account_id).get("blocks", [])
        except Exception:
            records = []
        for record in records:
            try:
                timestamp = datetime.fromisoformat(str(record["time"]))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if timestamp > cutoff:
                    recent += 1
            except (KeyError, TypeError, ValueError):
                continue
        return recent

    def _account_summary(
        self,
        account_id: str | None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = self.core.load_config(account_id) or {}
        record = record or {}
        runtime = self._runtime(account_id)
        return {
            **self._safe_account_record(record),
            "id": record.get("id") or account_id,
            "user_id": record.get("user_id") or cfg.get("user_id"),
            "username": record.get("username") or cfg.get("username", ""),
            "display_name": record.get("display_name", ""),
            "phone_masked": record.get("phone_masked", ""),
            "configured": bool(cfg.get("api_id") and cfg.get("user_id")),
            "blocked_count": int(cfg.get("blocked_count", 0) or 0),
            "kicked_count": int(cfg.get("kicked_count", 0) or 0),
            "recent_block_count": self._recent_block_count(account_id, cfg),
            "whitelist_count": len(cfg.get("whitelist", {}) or {}),
            "blacklist_count": len(cfg.get("blacklist", {}) or {}),
            "learned_keyword_count": len(
                (cfg.get("learned_patterns", {}) or {}).get("keywords", [])
            ),
            "last_scan": cfg.get("last_scan"),
            "running": bool(runtime and runtime.running),
            "ready": bool(runtime and runtime.ready and runtime.running),
            "state": runtime.state if runtime else "stopped",
            "error": runtime.error if runtime else None,
        }

    def _get_status(self, params: dict[str, Any]) -> dict[str, Any]:
        self.core.ensure_account_registry()
        records = self.core.list_accounts()
        active_account_id = self.core.get_active_account_id()
        selected_id = self._resolve_account_id(params)
        selected_record = next(
            (record for record in records if str(record.get("id")) == str(selected_id)),
            None,
        )
        selected = self._account_summary(selected_id, selected_record)
        return {
            "active_account_id": active_account_id,
            "selected_account": selected,
            "accounts": [
                self._account_summary(str(record.get("id")), record) for record in records
            ],
            "ocr": self.core.get_ocr_status(),
        }

    def _list_accounts(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.core.ensure_account_registry()
        return [self._safe_account_record(record) for record in self.core.list_accounts()]

    def _create_account(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = params.get("account_id")
        metadata = params.get("metadata")
        if account_id or metadata:
            return self.core.create_account(
                None if account_id in (None, "") else str(account_id),
                metadata=metadata if isinstance(metadata, dict) else None,
            )
        return self.core.create_account()

    def _remove_account(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        if not account_id:
            raise InvalidRequestError("account_id 不可為空")
        runtime = self._runtime(account_id)
        if runtime and runtime.running:
            raise RuntimeError("請先停止此帳號的即時防護")
        removed = self.core.remove_account(
            account_id,
            delete_files=self._coerce_bool(params.get("delete_files"), True),
        )
        self._listeners.pop(self._listener_key(account_id), None)
        return {"account_id": account_id, "removed": bool(removed)}

    def _get_account_details(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        cfg = self.core.load_config(account_id) or {}
        auto_start_account_ids: list[str] = []
        if hasattr(self.core, "get_auto_start_account_ids"):
            auto_start_account_ids = list(self.core.get_auto_start_account_ids() or [])
        elif hasattr(self.core, "get_auto_start_account_id"):
            legacy_id = self.core.get_auto_start_account_id()
            auto_start_account_ids = [legacy_id] if legacy_id else []
        if auto_start_account_ids:
            auto_start = account_id in auto_start_account_ids
        else:
            auto_start = bool(cfg.get("auto_start_protection"))
        auto_start_account_id = auto_start_account_ids[0] if auto_start_account_ids else None
        return {
            "account_id": account_id,
            "logged_in": bool(cfg.get("user_id")),
            "has_api_credentials": bool(cfg.get("api_id") and cfg.get("api_hash")),
            "managed_groups": list(cfg.get("managed_groups") or []),
            "scan_settings": self.core.get_scan_settings(account_id=account_id),
            "learned_patterns": self.core.get_learned_patterns(account_id=account_id),
            "auto_start": auto_start,
            "auto_start_account_id": auto_start_account_id,
            "auto_start_account_ids": auto_start_account_ids,
        }

    def _get_startup_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"enabled": bool(self.platform.is_start_on_login_enabled())}

    def _set_startup(self, params: dict[str, Any]) -> dict[str, Any]:
        enabled = self._coerce_bool(params.get("enabled"))
        self.platform.set_start_on_login(enabled)
        return {"enabled": bool(self.platform.is_start_on_login_enabled())}

    def _select_account(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = str(params.get("account_id") or "").strip()
        if not account_id:
            raise InvalidRequestError("account_id 不可為空")
        return self.core.set_active_account(account_id)

    def _set_auto_start(self, params: dict[str, Any]) -> dict[str, Any]:
        if "account_ids" in params:
            raw_ids = params.get("account_ids")
            if not isinstance(raw_ids, list):
                raise InvalidRequestError("account_ids 必須是 JSON array")
            if hasattr(self.core, "set_auto_start_accounts"):
                selected = self.core.set_auto_start_accounts([str(item) for item in raw_ids])
            else:
                raise InvalidRequestError("目前核心不支援多帳號自動啟動")
            return {"account_ids": list(selected)}

        account_id = params.get("account_id")
        selected = self.core.set_auto_start_account(
            None if account_id in (None, "") else str(account_id)
        )
        return {"account_id": selected, "account_ids": [selected] if selected else []}

    def _start_auth(self, params: dict[str, Any]) -> dict[str, Any]:
        api_id = str(params.get("api_id") or "").strip()
        api_hash = str(params.get("api_hash") or "").strip()
        phone = str(params.get("phone") or "").strip()
        if not api_id or not api_hash or not phone:
            raise InvalidRequestError("api_id、api_hash、phone 都不可為空")

        account_id = self._resolve_account_id(params)
        flow_id = uuid4().hex
        runtime = AuthRuntime(
            flow_id=flow_id,
            account_id=account_id,
            sensitive_values=[api_hash, phone],
        )
        with self._lock:
            self._sensitive_values.extend([api_hash, phone])
            if any(
                flow.account_id == account_id
                and flow.thread
                and flow.thread.is_alive()
                for flow in self._auth_flows.values()
            ):
                raise RuntimeError("此帳號已有登入流程進行中")
            self._auth_flows[flow_id] = runtime

        def run_auth() -> None:
            async def wait_for_value(value_queue: Queue[str], kind: str) -> str:
                self._emit_event(
                    {
                        "event": "auth_challenge",
                        "flow_id": flow_id,
                        "account_id": account_id,
                        "kind": kind,
                    }
                )
                while not runtime.cancel_event.is_set():
                    try:
                        return await asyncio.to_thread(value_queue.get, True, 0.2)
                    except Empty:
                        continue
                raise RuntimeError("登入流程已取消")

            async def code_callback() -> str:
                return await wait_for_value(runtime.code_queue, "code")

            async def password_callback() -> str:
                return await wait_for_value(runtime.password_queue, "password")

            def status_callback(delivery: str) -> None:
                self._emit_event(
                    {
                        "event": "auth_delivery",
                        "flow_id": flow_id,
                        "account_id": account_id,
                        "message": str(delivery),
                    }
                )

            try:
                me = asyncio.run(
                    self.core.authenticate(
                        api_id,
                        api_hash,
                        phone,
                        code_callback,
                        password_callback,
                        status_callback,
                        account_id,
                    )
                )
                runtime.state = "succeeded"
                self._emit_event(
                    {
                        "event": "auth_succeeded",
                        "flow_id": flow_id,
                        "account_id": account_id,
                        "user_id": getattr(me, "id", None),
                        "username": getattr(me, "username", None),
                        "display_name": " ".join(
                            value
                            for value in (
                                getattr(me, "first_name", None),
                                getattr(me, "last_name", None),
                            )
                            if value
                        ),
                    }
                )
            except Exception as exc:
                runtime.state = "cancelled" if runtime.cancel_event.is_set() else "failed"
                self._emit_event(
                    {
                        "event": "auth_failed",
                        "flow_id": flow_id,
                        "account_id": account_id,
                        "cancelled": runtime.cancel_event.is_set(),
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    },
                    extra_values=runtime.sensitive_values,
                )
            finally:
                with self._lock:
                    for value in runtime.sensitive_values:
                        try:
                            self._sensitive_values.remove(value)
                        except ValueError:
                            pass
                    self._auth_flows.pop(flow_id, None)

        runtime.thread = threading.Thread(
            target=run_auth,
            name=f"TeleShieldAuth-{flow_id[:8]}",
            daemon=True,
        )
        runtime.thread.start()
        return {
            "flow_id": flow_id,
            "account_id": account_id,
            "running": True,
        }

    def _auth_flow(self, params: dict[str, Any]) -> AuthRuntime:
        flow_id = str(params.get("flow_id") or "").strip()
        if not flow_id:
            raise InvalidRequestError("flow_id 不可為空")
        with self._lock:
            runtime = self._auth_flows.get(flow_id)
        if runtime is None or not runtime.thread or not runtime.thread.is_alive():
            raise RuntimeError("登入流程不存在或已結束")
        return runtime

    def _submit_auth_value(
        self,
        params: dict[str, Any],
        value_queue_name: str,
    ) -> dict[str, Any]:
        value = str(params.get("value") or "").strip()
        if not value:
            raise InvalidRequestError("登入輸入不可為空")
        runtime = self._auth_flow(params)
        runtime.sensitive_values.append(value)
        with self._lock:
            self._sensitive_values.append(value)
        getattr(runtime, value_queue_name).put_nowait(value)
        return {"flow_id": runtime.flow_id, "accepted": True}

    def _submit_auth_code(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._submit_auth_value(params, "code_queue")

    def _submit_auth_password(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._submit_auth_value(params, "password_queue")

    def _cancel_auth(self, params: dict[str, Any]) -> dict[str, Any]:
        runtime = self._auth_flow(params)
        runtime.cancel_event.set()
        return {"flow_id": runtime.flow_id, "cancelled": True}

    def _get_ocr_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.core.get_ocr_status()

    def _start_protection(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        key = self._listener_key(account_id)
        with self._lock:
            current = self._listeners.get(key)
            if current and current.running:
                return {
                    "account_id": account_id,
                    "running": True,
                    "ready": current.ready,
                    "state": current.state,
                }
            runtime = ListenerRuntime(account_id=account_id)
            self._listeners[key] = runtime
            self._emit_status(runtime)
            runtime.thread = threading.Thread(
                target=self._listener_worker,
                args=(runtime,),
                name=f"TeleShieldListener-{key}",
                daemon=True,
            )
            runtime.thread.start()
        return {
            "account_id": account_id,
            "running": True,
            "ready": False,
            "state": "starting",
        }

    def _stop_protection(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        key = self._listener_key(account_id)
        with self._lock:
            runtime = self._listeners.get(key)
        if runtime is None or not runtime.running:
            return {"account_id": account_id, "running": False}
        runtime.stop_event.set()
        if runtime.thread and runtime.thread is not threading.current_thread():
            runtime.thread.join(timeout=15)
        return {"account_id": account_id, "running": runtime.running}

    def _stop_all(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            runtimes = list(self._listeners.values())
        stopped = []
        for runtime in runtimes:
            runtime.stop_event.set()
        for runtime in runtimes:
            if runtime.thread and runtime.thread is not threading.current_thread():
                runtime.thread.join(timeout=15)
            stopped.append(runtime.account_id)
        return {"accounts": stopped}

    def _emit_status(self, runtime: ListenerRuntime) -> None:
        self._emit_event(
            {
                "event": "status",
                "account_id": runtime.account_id,
                "state": runtime.state,
                "running": runtime.running or runtime.state == "starting",
                "ready": runtime.ready,
                "error": runtime.error,
            }
        )

    def _listener_worker(self, runtime: ListenerRuntime) -> None:
        async def run_listener() -> bool:
            async_stop = asyncio.Event()

            async def watch_stop_request() -> None:
                while not runtime.stop_event.is_set():
                    await asyncio.sleep(0.05)
                async_stop.set()

            stop_task = asyncio.create_task(watch_stop_request())

            def ready_callback() -> None:
                runtime.ready = True
                runtime.state = "ready"
                self._emit_status(runtime)

            try:
                result = await self.core.listen(
                    stop_event=async_stop,
                    ready_callback=ready_callback,
                    account_id=runtime.account_id,
                )
                if not runtime.stop_event.is_set() and runtime.state != "error":
                    runtime.state = "error"
                    runtime.error = "核心 listener 意外結束"
                    self._emit_status(runtime)
                return bool(result)
            except Exception as exc:
                runtime.state = "error"
                runtime.error = str(exc)
                self._emit_status(runtime)
                return False
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)

        try:
            asyncio.run(run_listener())
        except Exception as exc:
            runtime.state = "error"
            runtime.error = _redact_text(str(exc))
            self._emit_status(runtime)
        finally:
            if runtime.stop_event.is_set() and runtime.state != "error":
                runtime.state = "stopped"
                runtime.ready = False
                self._emit_status(runtime)

    def _list_entries(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.core.list_entries(
            str(params.get("list_type") or ""),
            str(params.get("query") or ""),
            account_id=self._resolve_account_id(params),
        )

    def _upsert_list_entry(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.core.upsert_list_entry(
            str(params.get("list_type") or ""),
            str(params.get("user_id") or ""),
            str(params.get("username") or ""),
            str(params.get("reason") or "manual"),
            account_id=self._resolve_account_id(params),
        )

    def _remove_list_entry(self, params: dict[str, Any]) -> dict[str, Any]:
        removed = self.core.remove_list_entry(
            str(params.get("list_type") or ""),
            str(params.get("user_id") or ""),
            account_id=self._resolve_account_id(params),
        )
        return {"removed": bool(removed)}

    def _import_list(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "").strip()
        if not path:
            raise InvalidRequestError("path 不可為空")
        count = self.core.import_list_entries(
            path,
            str(params.get("list_type") or ""),
            replace=self._coerce_bool(params.get("replace")),
            account_id=self._resolve_account_id(params),
        )
        return {"count": int(count)}

    def _export_list(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "").strip()
        if not path:
            raise InvalidRequestError("path 不可為空")
        count = self.core.export_list_entries(
            path,
            str(params.get("list_type") or ""),
            str(params.get("fmt") or ""),
            account_id=self._resolve_account_id(params),
        )
        return {"count": int(count), "path": path}

    def _get_learned_patterns(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.core.get_learned_patterns(account_id=self._resolve_account_id(params))

    def _remove_learned_pattern(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind") or "")
        value = str(params.get("value") or "")
        if not kind or not value:
            raise InvalidRequestError("kind 與 value 不可為空")
        removed = self.core.remove_learned_pattern(
            kind,
            value,
            account_id=self._resolve_account_id(params),
        )
        return {"removed": bool(removed)}

    def _get_block_records(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.core.get_block_records(
            str(params.get("query") or ""),
            str(params.get("source") or "all"),
            int(params.get("limit") or 500),
            account_id=self._resolve_account_id(params),
        )

    def _export_blocks(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "").strip()
        if not path:
            raise InvalidRequestError("path 不可為空")
        count = self.core.export_block_records(
            path,
            str(params.get("query") or ""),
            str(params.get("source") or "all"),
            str(params.get("fmt") or "json"),
            account_id=self._resolve_account_id(params),
        )
        return {"count": int(count), "path": path}

    def _build_report(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.core.build_report(
            str(params.get("period") or "day"),
            account_id=self._resolve_account_id(params),
        )

    def _learn_text(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.core.learn_text(
            str(params.get("text") or ""),
            account_id=self._resolve_account_id(params),
        )

    def _get_scan_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.core.get_scan_settings(account_id=self._resolve_account_id(params))

    def _update_scan_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        updates = params.get("updates") or {}
        if not isinstance(updates, dict):
            raise InvalidRequestError("updates 必須是 JSON object")
        return self.core.update_scan_settings(
            updates,
            account_id=self._resolve_account_id(params),
        )

    def _start_async_job(
        self,
        operation: Callable[[], Any],
        event_name: str,
        account_id: str | None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        job_id = uuid4().hex
        cancel_event = threading.Event()

        def run_job() -> None:
            try:
                result = asyncio.run(operation())
                event = {
                    "event": event_name,
                    "job_id": job_id,
                    "account_id": account_id,
                    "result": result,
                }
                if operation_name:
                    event["operation"] = operation_name
                self._emit_event(event)
            except Exception as exc:
                event = {
                    "event": event_name.replace("_finished", "_failed"),
                    "job_id": job_id,
                    "account_id": account_id,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
                if operation_name:
                    event["operation"] = operation_name
                self._emit_event(event)
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)

        thread = threading.Thread(
            target=run_job,
            name=f"TeleShieldJob-{job_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._jobs[job_id] = (thread, cancel_event)
        thread.start()
        return {"job_id": job_id, "account_id": account_id, "running": True}

    def _discover_groups(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        return self._start_async_job(
            lambda: self.core.discover_managed_groups(account_id=account_id),
            "groups_finished",
            account_id,
        )

    def _set_group_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        group_id = str(params.get("group_id") or "").strip()
        if not group_id:
            raise InvalidRequestError("group_id 不可為空")
        updated = self.core.set_managed_group_enabled(
            group_id,
            self._coerce_bool(params.get("enabled")),
            account_id=self._resolve_account_id(params),
        )
        return {"updated": bool(updated), "group_id": group_id}

    def _logout(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        runtime = self._runtime(account_id)
        if runtime and runtime.running:
            raise RuntimeError("請先停止此帳號的即時防護")
        remove_credentials = self._coerce_bool(params.get("remove_credentials"))
        return self._start_async_job(
            lambda: self.core.logout_account(remove_credentials, account_id=account_id),
            "account_operation_finished",
            account_id,
            operation_name="logout",
        )

    def _clear_session(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        runtime = self._runtime(account_id)
        if runtime and runtime.running:
            raise RuntimeError("請先停止此帳號的即時防護")
        self.core.clear_local_session(
            remove_credentials=self._coerce_bool(params.get("remove_credentials")),
            account_id=account_id,
        )
        return {"account_id": account_id, "cleared": True}

    def _start_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        account_id = self._resolve_account_id(params)
        scan_key = self._listener_key(account_id)
        with self._lock:
            existing_id = self._scan_jobs.get(scan_key)
            if existing_id:
                existing = self._jobs.get(existing_id)
                if existing and existing[0].is_alive():
                    raise RuntimeError("此帳號已有歷史掃描進行中")
                self._scan_jobs.pop(scan_key, None)
            job_id = uuid4().hex
            cancel_event = threading.Event()
            self._scan_jobs[scan_key] = job_id

        def run_scan() -> None:
            def progress(message: str) -> None:
                self._emit_event(
                    {
                        "event": "scan_progress",
                        "job_id": job_id,
                        "message": str(message),
                    }
                )

            try:
                result = asyncio.run(
                    self.core.scan_history(
                        scope=str(params.get("scope") or "private"),
                        dry_run=self._coerce_bool(params.get("dry_run"), True),
                        progress_callback=progress,
                        cancel_event=cancel_event,
                        account_id=account_id,
                    )
                )
                self._emit_event(
                    {"event": "scan_finished", "job_id": job_id, "result": result}
                )
            except Exception as exc:
                self._emit_event(
                    {
                        "event": "scan_failed",
                        "job_id": job_id,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)
                    if self._scan_jobs.get(scan_key) == job_id:
                        self._scan_jobs.pop(scan_key, None)

        thread = threading.Thread(
            target=run_scan,
            name=f"TeleShieldScan-{job_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._jobs[job_id] = (thread, cancel_event)
        thread.start()
        return {"job_id": job_id, "account_id": account_id, "running": True}

    def _cancel_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = str(params.get("job_id") or "")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "cancelled": False}
        _, cancel_event = job
        cancel_event.set()
        return {"job_id": job_id, "cancelled": True}

    def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        self._shutdown_requested = True
        return self._stop_all(params)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        service = CoreService()
        print(
            json.dumps(
                {"ok": True, "ocr": service.dispatch("get_ocr_status")},
                ensure_ascii=False,
            )
        )
        return 0
    if "--stdio" not in argv:
        print("TeleShieldCore --stdio | --self-test", file=sys.stderr)
        return 2
    service = CoreService()
    service.run(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
