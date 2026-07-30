import io
import json
import threading
import time
from types import SimpleNamespace

from core_service import AuthRuntime, CoreService


class FakeCore:
    def __init__(self):
        self.accounts = [
            {
                "id": "account-a",
                "user_id": 101,
                "username": "alice",
                "display_name": "Alice",
                "phone_masked": "+12****90",
            }
        ]
        self.active_account_id = "account-a"
        self.config = {
            "api_id": 1234,
            "user_id": 101,
            "username": "alice",
            "blocked_count": 4,
            "kicked_count": 2,
            "whitelist": {"9": {}},
            "blacklist": {"8": {}},
        }
        self.stop_seen = threading.Event()

    def ensure_account_registry(self):
        return self.accounts

    def list_accounts(self):
        return list(self.accounts)

    def get_active_account_id(self):
        return self.active_account_id

    def load_config(self, account_id=None):
        return dict(self.config)

    def load_block_log(self, account_id=None):
        return {"blocks": []}

    def get_ocr_status(self):
        return {"available": False, "bundled": False, "languages": []}

    async def listen(self, stop_event=None, ready_callback=None, account_id=None):
        if ready_callback:
            ready_callback()
        await stop_event.wait()
        self.stop_seen.set()
        return True


def test_get_status_exposes_safe_core_summary_and_selected_account():
    service = CoreService(core=FakeCore())

    result = service.dispatch("get_status")

    assert result["active_account_id"] == "account-a"
    assert result["selected_account"]["username"] == "alice"
    assert result["selected_account"]["blocked_count"] == 4
    assert result["selected_account"]["kicked_count"] == 2
    assert result["selected_account"]["whitelist_count"] == 1
    assert result["selected_account"]["blacklist_count"] == 1
    assert result["selected_account"]["configured"] is True
    assert result["ocr"]["available"] is False
    assert "api_hash" not in result["selected_account"]


def test_stdio_protocol_returns_json_responses_and_structured_errors():
    service = CoreService(core=FakeCore())
    stdin = io.StringIO(
        '{"id": 1, "method": "list_accounts"}\n'
        '{"id": 2, "method": "does_not_exist"}\n'
    )
    stdout = io.StringIO()

    service.run(stdin, stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert responses[0] == {
        "id": 1,
        "ok": True,
        "result": [
            {
                "id": "account-a",
                "user_id": 101,
                "username": "alice",
                "display_name": "Alice",
                "phone_masked": "+12****90",
            }
        ],
    }
    assert responses[1]["id"] == 2
    assert responses[1]["ok"] is False
    assert responses[1]["error"]["type"] == "UnknownMethodError"


def test_start_and_stop_protection_emit_ready_event_and_join_worker():
    events = []
    core = FakeCore()
    service = CoreService(core=core, emit_event=events.append)

    started = service.dispatch("start_protection", {"account_id": "account-a"})
    assert started["account_id"] == "account-a"
    assert started["running"] is True

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(event.get("state") == "ready" for event in events):
        time.sleep(0.01)

    assert {event.get("state") for event in events} >= {"starting", "ready"}

    stopped = service.dispatch("stop_protection", {"account_id": "account-a"})
    assert stopped == {"account_id": "account-a", "running": False}
    assert core.stop_seen.wait(1)
    assert service.dispatch("get_status")["selected_account"]["running"] is False


def test_invalid_json_is_reported_without_breaking_following_requests():
    service = CoreService(core=FakeCore())
    stdin = io.StringIO("not-json\n{\"id\": 3, \"method\": \"get_ocr_status\"}\n")
    stdout = io.StringIO()

    service.run(stdin, stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert responses[0]["id"] is None
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["type"] == "InvalidRequestError"
    assert responses[1] == {
        "id": 3,
        "ok": True,
        "result": {"available": False, "bundled": False, "languages": []},
    }


class FakeAuthCore(FakeCore):
    async def authenticate(
        self,
        api_id,
        api_hash,
        phone,
        code_callback,
        password_callback,
        status_callback=None,
        account_id=None,
    ):
        if status_callback:
            status_callback("App")
        code = await code_callback()
        password = await password_callback()
        assert (api_id, api_hash, phone, code, password, account_id) == (
            "1234",
            "hash",
            "+100****0000",
            "2468",
            "secret",
            "account-a",
        )
        return SimpleNamespace(id=101, username="alice", first_name="Alice")


def test_auth_flow_round_trips_code_and_two_step_password_without_blocking_stdio():
    events = []
    service = CoreService(core=FakeAuthCore(), emit_event=events.append)

    started = service.dispatch(
        "start_auth",
        {
            "api_id": "1234",
            "api_hash": "hash",
            "phone": "+100****0000",
            "account_id": "account-a",
        },
    )
    flow_id = started["flow_id"]
    assert started["running"] is True

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event.get("event") == "auth_challenge" and event.get("kind") == "code"
        for event in events
    ):
        time.sleep(0.01)
    assert any(event.get("kind") == "code" for event in events)

    assert service.dispatch(
        "submit_auth_code", {"flow_id": flow_id, "value": "2468"}
    ) == {"flow_id": flow_id, "accepted": True}

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event.get("event") == "auth_challenge" and event.get("kind") == "password"
        for event in events
    ):
        time.sleep(0.01)
    assert any(event.get("kind") == "password" for event in events)

    assert service.dispatch(
        "submit_auth_password", {"flow_id": flow_id, "value": "secret"}
    ) == {"flow_id": flow_id, "accepted": True}

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(
        event.get("event") == "auth_succeeded" for event in events
    ):
        time.sleep(0.01)
    assert any(event.get("event") == "auth_succeeded" for event in events)


class FakePlatform:
    def __init__(self):
        self.enabled = False

    def is_start_on_login_enabled(self):
        return self.enabled

    def set_start_on_login(self, enabled):
        self.enabled = bool(enabled)


class FakeParityCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.startup_enabled = False
        self.learned = {"keywords": ["spam"], "patterns": ["https://"]}
        self.groups = [{"id": "-100", "title": "Announcements", "enabled": True}]
        self.calls = []

    def create_account(self):
        self.calls.append("create_account")
        return {"id": "account-b", "display_name": ""}

    def remove_account(self, account_id, delete_files=True):
        self.calls.append(("remove_account", account_id, delete_files))
        return True

    def get_learned_patterns(self, account_id=None):
        return dict(self.learned)

    def remove_learned_pattern(self, kind, value, account_id=None):
        self.calls.append(("remove_learned_pattern", kind, value))
        return True

    async def discover_managed_groups(self, account_id=None):
        self.calls.append(("discover_managed_groups", account_id))
        return self.groups

    def set_managed_group_enabled(self, group_id, enabled, account_id=None):
        self.calls.append(("set_managed_group_enabled", str(group_id), enabled))
        return True

    async def logout_account(self, remove_credentials=False, account_id=None):
        self.calls.append(("logout_account", remove_credentials, account_id))
        return True

    def clear_local_session(self, remove_credentials=False, account_id=None):
        self.calls.append(("clear_local_session", remove_credentials, account_id))

    def get_scan_settings(self, account_id=None):
        return {"private_dialog_limit": 1}

    def update_scan_settings(self, updates, account_id=None):
        self.calls.append(("update_scan_settings", updates, account_id))
        return dict(updates)

    def import_list_entries(self, path, list_type, replace=False, account_id=None):
        self.calls.append(("import_list_entries", path, list_type, replace, account_id))
        return 2

    def export_list_entries(self, path, list_type, fmt="", account_id=None):
        self.calls.append(("export_list_entries", path, list_type, fmt, account_id))
        return 3

    def export_block_records(self, path, query="", source="all", fmt="json", account_id=None):
        self.calls.append(("export_block_records", path, query, source, fmt, account_id))
        return 4

    def get_auto_start_account_id(self):
        return "account-a"


def _wait_for_event(events, name, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in events:
            if event.get("event") == name:
                return event
        time.sleep(0.01)
    raise AssertionError(f"event not emitted: {name}")


def test_management_surface_covers_account_rules_groups_exports_and_startup():
    events = []
    core = FakeParityCore()
    service = CoreService(core=core, emit_event=events.append, platform=FakePlatform())

    assert service.dispatch("create_account")["id"] == "account-b"
    assert service.dispatch("remove_account", {"account_id": "account-b"})["removed"] is True
    assert service.dispatch("get_learned_patterns")["keywords"] == ["spam"]
    assert service.dispatch(
        "remove_learned_pattern", {"kind": "keywords", "value": "spam"}
    )["removed"] is True
    assert service.dispatch("set_group_enabled", {"group_id": "-100", "enabled": False})["updated"] is True
    assert service.dispatch("get_startup_status")["enabled"] is False
    assert service.dispatch("set_startup", {"enabled": True})["enabled"] is True
    assert service.dispatch("import_list", {"path": "/tmp/in.json", "list_type": "blacklist"})["count"] == 2
    assert service.dispatch("export_list", {"path": "/tmp/out.json", "list_type": "blacklist"})["count"] == 3
    assert service.dispatch("export_blocks", {"path": "/tmp/log.csv", "fmt": "csv"})["count"] == 4
    assert service.dispatch("get_scan_settings")["private_dialog_limit"] == 1
    assert service.dispatch("update_scan_settings", {"updates": {"private_dialog_limit": 4}})["private_dialog_limit"] == 4

    group_job = service.dispatch("discover_groups")
    assert group_job["running"] is True
    assert _wait_for_event(events, "groups_finished")["result"] == core.groups

    logout_job = service.dispatch("logout", {"remove_credentials": False})
    assert logout_job["running"] is True
    assert _wait_for_event(events, "account_operation_finished")["operation"] == "logout"


def test_account_details_exposes_global_auto_start_and_omits_credentials():
    service = CoreService(core=FakeParityCore(), platform=FakePlatform())

    details = service.dispatch("get_account_details", {"account_id": "account-a"})

    assert details["account_id"] == "account-a"
    assert details["auto_start"] is True
    assert details["auto_start_account_id"] == "account-a"
    assert "api_id" not in details
    assert "api_hash" not in details


def test_scan_protocol_preserves_explicit_false_dry_run():
    class ScanCore(FakeCore):
        def __init__(self):
            super().__init__()
            self.seen_dry_run = None

        async def scan_history(
            self,
            scope,
            dry_run=True,
            progress_callback=None,
            cancel_event=None,
            account_id=None,
        ):
            self.seen_dry_run = dry_run
            if progress_callback:
                progress_callback("scan started")
            return {"scope": scope, "dry_run": dry_run, "findings": []}

    events = []
    core = ScanCore()
    service = CoreService(core=core, emit_event=events.append)
    job = service.dispatch("start_scan", {"scope": "private", "dry_run": "false"})
    assert job["running"] is True
    result = _wait_for_event(events, "scan_finished")["result"]
    assert result["dry_run"] is False
    assert core.seen_dry_run is False


def test_unexpected_listener_exit_cleans_runtime_thread():
    class UnexpectedExitCore(FakeCore):
        async def listen(self, stop_event=None, ready_callback=None, account_id=None):  # type: ignore[override]
            return False

    events = []
    service = CoreService(core=UnexpectedExitCore(), emit_event=events.append)
    service.dispatch("start_protection", {"account_id": "account-a"})
    runtime = service._listeners["account-a"]
    _wait_for_event(events, "status")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and runtime.thread and runtime.thread.is_alive():
        time.sleep(0.01)
    try:
        assert runtime.thread is not None
        assert runtime.thread.is_alive() is False
    finally:
        runtime.stop_event.set()
        if runtime.thread:
            runtime.thread.join(timeout=1)


def test_auth_error_and_event_log_redact_credentials():
    class SecretErrorCore(FakeCore):
        async def authenticate(
            self,
            api_id,
            api_hash,
            phone,
            code_callback,
            password_callback,
            status_callback=None,
            account_id=None,
        ):
            raise RuntimeError(
                "api_hash=super-secret-hash password=two-step-pass phone=+15551234567"
            )

    events = []
    service = CoreService(core=SecretErrorCore(), emit_event=events.append)
    service.dispatch(
        "start_auth",
        {
            "api_id": "1234",
            "api_hash": "super-secret-hash",
            "phone": "+15551234567",
            "account_id": "account-a",
        },
    )
    failure = _wait_for_event(events, "auth_failed")
    serialized = json.dumps(failure, ensure_ascii=False)
    assert "super-secret-hash" not in serialized
    assert "two-step-pass" not in serialized
    assert "+15551234567" not in serialized
    assert "[REDACTED]" in serialized


def test_close_joins_auth_and_job_workers():
    class SpyThread:
        def __init__(self):
            self.join_calls = []

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_calls.append(timeout)

    service = CoreService(core=FakeCore())
    auth_thread = SpyThread()
    job_thread = SpyThread()
    auth = service._auth_flows["flow"] = AuthRuntime(  # type: ignore[arg-type]
        flow_id="flow", account_id="account-a", thread=auth_thread
    )
    job_cancel = threading.Event()
    service._jobs["job"] = (job_thread, job_cancel)  # type: ignore[assignment]

    service.close()

    assert auth.cancel_event.is_set()
    assert job_cancel.is_set()
    assert auth_thread.join_calls
    assert job_thread.join_calls
