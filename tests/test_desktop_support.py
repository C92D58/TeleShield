import asyncio
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import telethon
import teleshield
from desktop_platform import application_command
from telethon.tl.types import Channel, User


def configure_temp_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(teleshield, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(teleshield, "SESSION_FILE", tmp_path / "user.session")
    monkeypatch.setattr(teleshield, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(teleshield, "BLOCK_LOG", tmp_path / "block_log.json")


def _identity_update_process(root, account_id, start_event, results):
    """Widen the pre-write race so cross-process registry locking is deterministic."""
    import time

    original_write = teleshield._write_account_registry

    def slow_write(data, root=None):
        time.sleep(0.2)
        return original_write(data, root)

    teleshield._write_account_registry = slow_write
    start_event.wait(10)
    try:
        teleshield.update_account_identity(
            account_id,
            SimpleNamespace(id=9090, username="shared", first_name="Shared"),
            root=Path(root),
        )
        results.put("ok")
    except ValueError:
        results.put("duplicate")
    except Exception as exc:
        results.put(f"error:{type(exc).__name__}:{exc}")


def test_default_session_dir_honours_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TELESHIELD_DATA_DIR", str(tmp_path / "data"))
    assert teleshield.default_session_dir() == tmp_path / "data"


def test_cli_lists_use_the_same_keys_as_runtime(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)

    asyncio.run(teleshield.manage_list("add", "whitelist", "123456"))
    asyncio.run(teleshield.manage_list("add", "blacklist", "987654"))

    cfg = teleshield.load_config()
    assert "whitelist" in cfg
    assert "blacklist" in cfg
    assert "whitelist_list" not in cfg
    assert "blacklist_list" not in cfg
    assert teleshield.is_whitelisted(123456, cfg)
    assert teleshield.is_blacklisted(987654, cfg)


def test_authenticate_reports_telegram_code_delivery(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)

    class SentCodeTypeApp:
        pass

    class FakeClient:
        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            return SimpleNamespace(type=SentCodeTypeApp())

        async def sign_in(self, **kwargs):
            pass

        async def get_me(self):
            return SimpleNamespace(id=42, username="test_user", first_name="Test")

    delivery = []
    with patch.object(telethon, "TelegramClient", FakeClient):
        me = asyncio.run(
            teleshield.authenticate(
                "1234",
                "[REDACTED]",
                "+10000000000",
                lambda: asyncio.sleep(0, result="[REDACTED]"),
                lambda: asyncio.sleep(0, result="[REDACTED]"),
                delivery.append,
            )
        )

    assert me.id == 42
    assert delivery == ["App"]
    assert teleshield.load_config()["user_id"] == 42


def test_desktop_launch_command_starts_hidden():
    command = application_command()
    assert command[-1] == "--background"
    assert command


def test_scan_history_private_dry_run_reports_match_without_blocking(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({
        "api_id": 1234,
        "api_hash": "[REDACTED]",
        "user_id": 1,
        "blocked_count": 0,
        "whitelist": {},
        "blacklist": {},
    })

    spammer = User(id=2, is_self=False, bot=False, first_name="Spam")

    class FakeClient:
        def __init__(self, *args):
            self.block_requests = 0

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_dialogs(self, limit):
            assert limit == 30
            return [SimpleNamespace(entity=spammer)]

        async def get_messages(self, entity, limit):
            assert entity is spammer
            assert limit == 5
            return [SimpleNamespace(
                date=datetime.now(timezone.utc),
                text="投資穩賺，立即加入",
                photo=None,
            )]

        async def __call__(self, request):
            return SimpleNamespace(users=[])

    with patch.object(telethon, "TelegramClient", FakeClient):
        result = asyncio.run(teleshield.scan_history("private", dry_run=True))

    assert result["matched"] == 1
    assert result["acted"] == 0
    assert result["dry_run"] is True
    assert result["cancelled"] is False
    assert teleshield.load_config()["blocked_count"] == 0


def test_scan_history_private_applies_block_and_records_result(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({
        "api_id": 1234,
        "api_hash": "[REDACTED]",
        "user_id": 1,
        "blocked_count": 0,
        "whitelist": {},
        "blacklist": {},
    })

    spammer = User(id=2, is_self=False, bot=False, first_name="Spam")

    class FakeClient:
        block_requests = 0

        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_dialogs(self, limit):
            return [SimpleNamespace(entity=spammer)]

        async def get_messages(self, entity, limit):
            return [SimpleNamespace(
                date=datetime.now(timezone.utc),
                text="投資穩賺，立即加入",
                photo=None,
            )]

        async def __call__(self, request):
            if type(request).__name__ == "BlockRequest":
                type(self).block_requests += 1
            return SimpleNamespace(users=[])

    with patch.object(telethon, "TelegramClient", FakeClient):
        result = asyncio.run(teleshield.scan_history("private", dry_run=False))

    assert result["matched"] == 1
    assert result["acted"] == 1
    assert FakeClient.block_requests == 1
    assert teleshield.load_config()["blocked_count"] == 1
    assert teleshield.load_block_log()["blocks"][0]["source"] == "scan"


def test_scan_history_group_dry_run_reports_finding_without_kicking(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({
        "api_id": 1234,
        "api_hash": "[REDACTED]",
        "user_id": 1,
        "blocked_count": 0,
        "kicked_count": 0,
        "whitelist": {},
        "blacklist": {},
    })

    me = User(id=1, is_self=True, bot=False, first_name="Owner")
    spammer = User(id=2, is_self=False, bot=False, first_name="Spam")
    group = Channel(
        id=99,
        title="Test Group",
        photo=None,
        date=datetime.now(timezone.utc),
        broadcast=False,
        megagroup=True,
    )
    message = SimpleNamespace(
        sender_id=spammer.id,
        date=datetime.now(timezone.utc),
        text="投資穩賺，立即加入",
        photo=None,
    )

    class FakeClient:
        kick_requests = 0

        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return me

        async def get_dialogs(self, limit):
            assert limit == 50
            return [SimpleNamespace(entity=group)]

        async def get_permissions(self, entity, user_id):
            assert entity is group
            assert user_id == me.id
            return SimpleNamespace(is_admin=True)

        async def get_messages(self, entity, limit):
            assert entity is group
            assert limit == 20
            return [message]

        async def get_entity(self, user_id):
            assert user_id == spammer.id
            return spammer

        async def __call__(self, request):
            if type(request).__name__ == "EditBannedRequest":
                type(self).kick_requests += 1
            return SimpleNamespace(users=[])

    with patch.object(telethon, "TelegramClient", FakeClient):
        result = asyncio.run(teleshield.scan_history("group", dry_run=True))

    assert result["groups_found"] == 1
    assert result["matched"] == 1
    assert result["acted"] == 0
    assert FakeClient.kick_requests == 0
    assert teleshield.load_config()["kicked_count"] == 0


def test_scan_history_honours_cancellation_before_history_fetch(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({"api_id": 1234, "api_hash": "[REDACTED]"})
    cancel_event = threading.Event()
    cancel_event.set()

    class FakeClient:
        history_requested = False

        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def __call__(self, request):
            return SimpleNamespace(users=[])

        async def get_dialogs(self, limit):
            type(self).history_requested = True
            return []

    with patch.object(telethon, "TelegramClient", FakeClient):
        result = asyncio.run(
            teleshield.scan_history("private", dry_run=True, cancel_event=cancel_event)
        )

    assert result["cancelled"] is True
    assert FakeClient.history_requested is False


def test_learn_text_returns_changes_and_persists_rules(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({"learned_patterns": {"keywords": [], "patterns": []}})

    result = teleshield.learn_text("加微信 spam_account 投資穩賺")

    assert result["added_keywords"]
    assert result["added_patterns"]
    cfg = teleshield.load_config()
    assert "投資穩賺" in cfg["learned_patterns"]["keywords"]
    learned_file = teleshield._legacy_account_store().learned_patterns_file
    assert json.loads(learned_file.read_text(encoding="utf-8")) == cfg["learned_patterns"]
    assert teleshield.is_spam("這是投資穩賺廣告", cfg)


def test_learned_pattern_listing_and_removal(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({
        "learned_patterns": {"keywords": ["投資"], "patterns": ["spam\\d+"]}
    })

    assert teleshield.get_learned_patterns() == {
        "keywords": ["投資"],
        "patterns": ["spam\\d+"],
    }
    assert teleshield.remove_learned_pattern("keywords", "投資") is True
    assert teleshield.remove_learned_pattern("patterns", "missing") is False
    assert teleshield.get_learned_patterns() == {"keywords": [], "patterns": ["spam\\d+"]}


def test_sidecar_learned_patterns_are_used_by_spam_enforcement(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.save_config(
        {"learned_patterns": {"keywords": [], "patterns": []}},
        account_id="account-a",
    )
    teleshield.save_learned_patterns(
        {"keywords": ["side-only"], "patterns": []},
        account_id="account-a",
    )

    assert teleshield.is_spam("這是 side-only 廣告", teleshield.load_config("account-a")) is True


def test_build_report_returns_structured_summary_and_trend(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    now = datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
    teleshield.save_block_log({
        "blocks": [
            {
                "user_id": 1,
                "name": "Private Spam",
                "reason": "投資穩賺",
                "source": "private",
                "time": (now - timedelta(hours=1)).isoformat(),
            },
            {
                "user_id": 2,
                "name": "Group Spam",
                "reason": "加微信",
                "source": "group",
                "time": (now - timedelta(days=2)).isoformat(),
            },
            {
                "user_id": 3,
                "name": "Old Spam",
                "reason": "過期",
                "source": "private",
                "time": (now - timedelta(days=10)).isoformat(),
            },
        ]
    })

    result = teleshield.build_report("week", now=now)

    assert result["period"] == "week"
    assert result["total"] == 2
    assert result["by_source"] == {"private": 1, "group": 1}
    assert result["trend"] == {"2026-01-06": 1, "2026-01-08": 1}
    assert len(result["records"]) == 2


def test_list_entries_filter_and_update_scan_settings(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.upsert_list_entry("whitelist", "123", "trusted_user", "manual")
    teleshield.upsert_list_entry("blacklist", "456", "bad_user", "spam")

    assert teleshield.list_entries("whitelist", "trusted") == [
        {"user_id": "123", "username": "trusted_user", "added": teleshield.load_config()["whitelist"]["123"]["added"], "reason": "manual"}
    ]
    teleshield.remove_list_entry("blacklist", "456")
    assert teleshield.list_entries("blacklist") == []

    settings = teleshield.update_scan_settings({
        "private_dialog_limit": 9999,
        "private_message_limit": 0,
        "private_days": 30,
        "group_dialog_limit": 12,
        "group_message_limit": 40,
        "group_days": 2,
    })
    assert settings == {
        "private_dialog_limit": 100,
        "private_message_limit": 1,
        "private_days": 30,
        "group_dialog_limit": 12,
        "group_message_limit": 40,
        "group_days": 2,
    }
    assert teleshield.get_scan_settings()["private_dialog_limit"] == 100


def test_list_entries_can_export_and_import_json_and_csv(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.upsert_list_entry("whitelist", "123", "trusted_user", "manual")
    json_path = tmp_path / "whitelist.json"
    csv_path = tmp_path / "whitelist.csv"

    assert teleshield.export_list_entries(str(json_path), "whitelist") == 1
    assert teleshield.export_list_entries(str(csv_path), "whitelist") == 1

    teleshield.clear_local_session()
    assert teleshield.import_list_entries(str(json_path), "whitelist", replace=True) == 1
    assert teleshield.list_entries("whitelist")[0]["username"] == "trusted_user"


def test_managed_groups_merge_and_toggle(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({"managed_groups": [{"id": "1", "title": "Existing", "enabled": False}]})

    groups = teleshield.merge_managed_groups([
        {"id": "1", "title": "Renamed", "username": "existing"},
        {"id": "2", "title": "New Group", "username": "new_group"},
    ])

    assert groups[0]["enabled"] is False
    assert groups[0]["title"] == "Renamed"
    assert groups[1]["enabled"] is True
    teleshield.set_managed_group_enabled("2", False)
    assert teleshield.is_group_enabled("2", teleshield.load_config()) is False


def test_clear_local_session_removes_auth_identity_without_logging_secrets(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"fake-session")
    teleshield.save_config({
        "api_id": 1234,
        "api_hash": "[REDACTED]",
        "phone": "+100****0000",
        "user_id": 42,
        "username": "test_user",
        "blocked_count": 3,
        "learned_patterns": {"keywords": [], "patterns": []},
    })

    teleshield.clear_local_session(remove_credentials=True)

    assert not teleshield.SESSION_FILE.exists()
    cfg = teleshield.load_config()
    assert "api_id" not in cfg
    assert "api_hash" not in cfg
    assert "phone" not in cfg
    assert "user_id" not in cfg
    assert cfg["blocked_count"] == 3


def test_ocr_status_uses_configured_executable_without_logging_path(monkeypatch, tmp_path):
    executable = tmp_path / "tesseract"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("TELESHIELD_TESSERACT_PATH", str(executable))

    status = teleshield.get_ocr_status()

    assert status["available"] is True
    assert status["bundled"] is False
    assert status["languages"] == ["chi_sim", "eng"]


def test_discover_managed_groups_returns_only_admin_groups(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.save_config({"api_id": 1234, "api_hash": "[REDACTED]"})
    me = User(id=1, is_self=True, bot=False, first_name="Owner")
    admin_group = Channel(
        id=99,
        title="Admin Group",
        photo=None,
        date=datetime.now(timezone.utc),
        broadcast=False,
        megagroup=True,
    )
    ordinary_group = Channel(
        id=100,
        title="Other Group",
        photo=None,
        date=datetime.now(timezone.utc),
        broadcast=False,
        megagroup=True,
    )

    class FakeClient:
        def __init__(self, *args):
            self.logged_out = False

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return me

        async def get_dialogs(self, limit):
            assert limit == 100
            return [
                SimpleNamespace(entity=admin_group),
                SimpleNamespace(entity=ordinary_group),
            ]

        async def get_permissions(self, entity, user_id):
            return SimpleNamespace(is_admin=entity is admin_group, is_creator=False)

    with patch.object(telethon, "TelegramClient", FakeClient):
        groups = asyncio.run(teleshield.discover_managed_groups())

    assert groups == [{
        "id": "99",
        "title": "Admin Group",
        "username": "",
        "is_creator": False,
        "is_admin": True,
        "enabled": True,
    }]
    assert teleshield.load_config()["managed_groups"][0]["enabled"] is True


def test_logout_account_calls_telegram_and_clears_local_identity(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"fake-session")
    teleshield.save_config({
        "api_id": 1234,
        "api_hash": "[REDACTED]",
        "user_id": 42,
        "username": "test_user",
    })

    class FakeClient:
        logged_out = False

        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def log_out(self):
            type(self).logged_out = True

    with patch.object(telethon, "TelegramClient", FakeClient):
        asyncio.run(teleshield.logout_account(remove_credentials=True))

    assert FakeClient.logged_out is True
    assert not teleshield.SESSION_FILE.exists()
    assert "user_id" not in teleshield.load_config()
    assert "api_hash" not in teleshield.load_config()


def test_create_account_initializes_isolated_json_files(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")

    assert store.root.is_dir()
    assert json.loads(store.config_file.read_text(encoding="utf-8")) == {}
    assert json.loads(store.block_log.read_text(encoding="utf-8")) == {"blocks": []}
    assert json.loads(store.learned_patterns_file.read_text(encoding="utf-8")) == {
        "keywords": [],
        "patterns": [],
    }
    for path in (store.config_file, store.block_log, store.learned_patterns_file):
        assert path.stat().st_mode & 0o777 == 0o600


def test_account_storage_isolated_for_config_session_and_logs(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)

    account_a = teleshield.create_account("account-a")
    account_b = teleshield.create_account("account-b")
    teleshield.save_config({"user_id": 101, "username": "alpha"}, account_id="account-a")
    teleshield.save_config({"user_id": 202, "username": "beta"}, account_id="account-b")
    teleshield.save_block_log({"blocks": [{"user_id": 101, "source": "private"}]}, account_id="account-a")
    teleshield.save_block_log({"blocks": [{"user_id": 202, "source": "group"}]}, account_id="account-b")

    assert account_a["id"] == "account-a"
    assert account_b["id"] == "account-b"
    assert teleshield.load_config(account_id="account-a")["user_id"] == 101
    assert teleshield.load_config(account_id="account-b")["user_id"] == 202
    assert teleshield.load_block_log(account_id="account-a")["blocks"][0]["user_id"] == 101
    assert teleshield.load_block_log(account_id="account-b")["blocks"][0]["user_id"] == 202
    assert teleshield.account_store("account-a").session_file != teleshield.account_store("account-b").session_file
    assert teleshield.account_store("account-a").config_file.parent != teleshield.account_store("account-b").config_file.parent


def test_active_account_context_selects_only_one_default_without_cross_contamination(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    teleshield.save_config({"user_id": 101}, account_id="account-a")
    teleshield.save_config({"user_id": 202}, account_id="account-b")

    teleshield.set_active_account("account-b")
    assert teleshield.get_active_account_id() == "account-b"
    assert teleshield.load_config()["user_id"] == 202

    with teleshield.account_context("account-a"):
        assert teleshield.load_config()["user_id"] == 101
        teleshield.save_config({"user_id": 111})

    assert teleshield.load_config(account_id="account-a")["user_id"] == 111
    assert teleshield.load_config(account_id="account-b")["user_id"] == 202


def test_legacy_single_account_is_migrated_to_isolated_account_store(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"legacy-session")
    teleshield.save_config({
        "api_id": 1234,
        "api_hash": "[REDACTED]",
        "phone": "+100****0000",
        "user_id": 42,
        "username": "legacy_user",
    })
    teleshield.save_block_log({"blocks": [{"user_id": 7, "source": "private"}]})

    records = teleshield.ensure_account_registry()

    assert len(records) == 1
    account_id = records[0]["id"]
    assert records[0]["user_id"] == 42
    assert teleshield.get_active_account_id() == account_id
    store = teleshield.account_store(account_id)
    assert store.session_file.read_bytes() == b"legacy-session"
    assert teleshield.load_config()["username"] == "legacy_user"
    assert teleshield.load_block_log()["blocks"][0]["user_id"] == 7
    assert not teleshield.SESSION_FILE.exists()
    assert not teleshield.CONFIG_FILE.exists()


def test_authenticate_uses_the_selected_account_session_path(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    session_paths = []

    class FakeClient:
        def __init__(self, session, *args):
            session_paths.append(session)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            return SimpleNamespace(type=SimpleNamespace())

        async def sign_in(self, **kwargs):
            pass

        async def get_me(self):
            return SimpleNamespace(id=len(session_paths), username="account_user", first_name="Account")

    with patch.object(telethon, "TelegramClient", FakeClient):
        for account_id in ("account-a", "account-b"):
            asyncio.run(
                teleshield.authenticate(
                    "1234",
                    "[REDACTED]",
                    "+100****0000",
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    account_id=account_id,
                )
            )

    assert len(session_paths) == 2
    assert session_paths[0] != session_paths[1]
    assert str(teleshield.account_store("account-a").session_file) == session_paths[0]
    assert str(teleshield.account_store("account-b").session_file) == session_paths[1]


def test_authenticate_rejects_duplicate_identity_without_overwriting_existing_account(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    teleshield.save_config({"user_id": 101, "username": "account-a"}, account_id="account-a")
    teleshield.save_config({"user_id": 202, "username": "account-b"}, account_id="account-b")
    teleshield.update_account_identity(
        "account-a",
        SimpleNamespace(id=101, username="account-a", first_name="A"),
    )
    teleshield.update_account_identity(
        "account-b",
        SimpleNamespace(id=202, username="account-b", first_name="B"),
    )
    original_session = teleshield.account_store("account-a").session_file
    original_session.write_bytes(b"original-account-a-session")

    class FakeClient:
        def __init__(self, session, *args):
            self.session = Path(session)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            return SimpleNamespace(type=SimpleNamespace())

        async def sign_in(self, **kwargs):
            self.session.write_bytes(b"duplicate-account-b-session")

        async def get_me(self):
            return SimpleNamespace(id=202, username="account-b", first_name="B")

    with patch.object(telethon, "TelegramClient", FakeClient):
        with pytest.raises(ValueError, match="已經存在"):
            asyncio.run(
                teleshield.authenticate(
                    "1234",
                    "[REDACTED]",
                    "+100****0000",
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    account_id="account-a",
                )
            )

    assert teleshield.load_config(account_id="account-a")["user_id"] == 101
    assert teleshield.get_account("account-a")["user_id"] == 101
    assert original_session.read_bytes() == b"original-account-a-session"


def test_fresh_duplicate_identity_rejection_removes_new_session_and_identity_config(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    teleshield.update_account_identity(
        "account-a",
        SimpleNamespace(id=101, username="account-a", first_name="A"),
    )

    class FakeClient:
        def __init__(self, session, *args):
            self.session = Path(session)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            return SimpleNamespace(type=SimpleNamespace())

        async def sign_in(self, **kwargs):
            self.session.write_bytes(b"new-duplicate-session")

        async def get_me(self):
            return SimpleNamespace(id=101, username="account-a", first_name="A")

    with patch.object(telethon, "TelegramClient", FakeClient):
        with pytest.raises(ValueError, match="已經存在"):
            asyncio.run(
                teleshield.authenticate(
                    "1234",
                    "[REDACTED]",
                    "+100****0000",
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    account_id="account-b",
                )
            )

    assert not teleshield.account_store("account-b").session_file.exists()
    assert teleshield.load_config("account-b").get("user_id") is None
    assert teleshield.get_account("account-b").get("user_id") is None


def test_create_account_rejects_duplicate_metadata_identity(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.update_account_identity(
        "account-a",
        SimpleNamespace(id=101, username="account-a", first_name="A"),
    )

    with pytest.raises(ValueError, match="已經存在"):
        teleshield.create_account("account-b", metadata={"user_id": 101})

    assert [record["id"] for record in teleshield.list_accounts()] == ["account-a"]


def test_logout_one_account_does_not_touch_another_account(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    for account_id, user_id in (("account-a", 101), ("account-b", 202)):
        teleshield.save_config({
            "api_id": 1234,
            "api_hash": "[REDACTED]",
            "phone": "+100****0000",
            "user_id": user_id,
            "username": account_id,
        }, account_id=account_id)
        teleshield.update_account_identity(
            account_id,
            SimpleNamespace(id=user_id, username=account_id, first_name=account_id),
            "+100****0000",
        )
        teleshield.account_store(account_id).session_file.write_bytes(account_id.encode())

    class FakeClient:
        def __init__(self, session, *args):
            self.session = session

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def log_out(self):
            pass

    with patch.object(telethon, "TelegramClient", FakeClient):
        assert asyncio.run(teleshield.logout_account(remove_credentials=True, account_id="account-a")) is True

    assert not teleshield.account_store("account-a").session_file.exists()
    assert teleshield.account_store("account-b").session_file.read_bytes() == b"account-b"
    assert "api_id" not in teleshield.load_config(account_id="account-a")
    assert teleshield.load_config(account_id="account-b")["user_id"] == 202
    assert teleshield.get_account("account-b")["user_id"] == 202


def test_remove_account_keeps_registry_when_data_deletion_fails(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")

    def fail_rmtree(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(teleshield.shutil, "rmtree", fail_rmtree)

    assert teleshield.remove_account("account-a") is False
    assert teleshield.get_account("account-a") is not None


def test_authenticate_binds_identity_update_to_store_selected_before_await(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    teleshield.set_active_account("account-a")

    class FakeClient:
        def __init__(self, *args):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            return SimpleNamespace(type=SimpleNamespace())

        async def sign_in(self, **kwargs):
            pass

        async def get_me(self):
            return SimpleNamespace(id=101, username="account-a", first_name="A")

    async def code_callback():
        teleshield.set_active_account("account-b")
        return "[REDACTED]"

    with patch.object(telethon, "TelegramClient", FakeClient):
        asyncio.run(
            teleshield.authenticate(
                "1234",
                "[REDACTED]",
                "+100****0000",
                code_callback,
                lambda: asyncio.sleep(0, result="[REDACTED]"),
            )
        )

    assert teleshield.get_account("account-a")["user_id"] == 101
    assert teleshield.get_account("account-b")["user_id"] is None


def test_relogin_with_different_phone_does_not_reuse_authorized_session(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.save_config(
        {
            "api_id": 1234,
            "api_hash": "[REDACTED]",
            "phone": "+1111111111",
            "user_id": 101,
        },
        account_id="account-a",
    )
    teleshield.update_account_identity(
        "account-a",
        SimpleNamespace(id=101, username="old", first_name="Old"),
    )
    old_session = teleshield.account_store("account-a").session_file
    old_session.write_bytes(b"old-authorized-session")
    observed = []

    class FakeClient:
        def __init__(self, session, *args):
            self.session = Path(session)
            observed.append(self.session.exists())

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def send_code_request(self, phone):
            return SimpleNamespace(type=SimpleNamespace())

        async def sign_in(self, **kwargs):
            self.session.write_bytes(b"new-authorized-session")

        async def get_me(self):
            return SimpleNamespace(id=202, username="new", first_name="New")

    with patch.object(telethon, "TelegramClient", FakeClient):
        asyncio.run(
            teleshield.authenticate(
                "1234",
                "[REDACTED]",
                "+2222222222",
                lambda: asyncio.sleep(0, result="[REDACTED]"),
                lambda: asyncio.sleep(0, result="[REDACTED]"),
                account_id="account-a",
            )
        )

    assert observed == [False]
    assert teleshield.load_config("account-a")["user_id"] == 202
    assert teleshield.load_config("account-a")["phone"] == "+2222222222"


def test_concurrent_listeners_keep_account_context_and_session_isolated(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    for account_id, user_id in (("account-a", 101), ("account-b", 202)):
        teleshield.save_config({
            "api_id": 1234,
            "api_hash": "[REDACTED]",
            "user_id": user_id,
            "username": account_id,
            "whitelist": {},
            "blacklist": {},
        }, account_id=account_id)

    session_paths = []

    class FakeClient:
        def __init__(self, session, *args):
            self.session = session
            self.disconnected = asyncio.Event()
            session_paths.append(session)

        def on(self, event):
            return lambda handler: handler

        async def connect(self):
            Path(self.session).write_bytes(b"listener-session")
            Path(self.session).chmod(0o644)

        async def is_user_authorized(self):
            return True

        async def run_until_disconnected(self):
            await self.disconnected.wait()

        async def disconnect(self):
            self.disconnected.set()

    async def run_listener(account_id):
        stop_event = asyncio.Event()
        result = await teleshield.listen(
            stop_event=stop_event,
            ready_callback=stop_event.set,
            account_id=account_id,
        )
        return result

    async def run_all():
        return await asyncio.gather(run_listener("account-a"), run_listener("account-b"))

    with patch.object(telethon, "TelegramClient", FakeClient):
        results = asyncio.run(run_all())

    assert results == [True, True]
    assert sorted(session_paths) == sorted([
        str(teleshield.account_store("account-a").session_file),
        str(teleshield.account_store("account-b").session_file),
    ])
    for account_id in ("account-a", "account-b"):
        assert teleshield.account_store(account_id).session_file.stat().st_mode & 0o777 == 0o600


def test_account_store_hardens_account_directory_and_known_data_files(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    store.ensure()
    for path in (
        store.session_file,
        Path(f"{store.session_file}-journal"),
        store.config_file,
        store.block_log,
        store.learned_patterns_file,
    ):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o644)

    store.ensure()

    assert store.root.stat().st_mode & 0o777 == 0o700
    for path in (
        store.session_file,
        Path(f"{store.session_file}-journal"),
        store.config_file,
        store.block_log,
        store.learned_patterns_file,
    ):
        assert path.stat().st_mode & 0o777 == 0o600


def test_account_store_hardens_data_root_and_accounts_parent(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    root = tmp_path / "nested-data"
    teleshield.create_account("account-a", root=root)

    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "accounts").stat().st_mode & 0o777 == 0o700


def test_existing_registry_accounts_are_rehardened_by_registry_initialization(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    for path, mode in (
        (tmp_path, 0o755),
        (tmp_path / "accounts", 0o755),
        (store.root, 0o755),
        (store.config_file, 0o644),
        (store.block_log, 0o644),
        (store.learned_patterns_file, 0o644),
    ):
        path.chmod(mode)

    teleshield.ensure_account_registry()

    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "accounts").stat().st_mode & 0o777 == 0o700
    assert store.root.stat().st_mode & 0o777 == 0o700
    for path in (store.config_file, store.block_log, store.learned_patterns_file):
        assert path.stat().st_mode & 0o777 == 0o600


def test_legacy_migration_rolls_back_registry_and_retries_after_copy_failure(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"legacy-session")
    teleshield.save_config({"user_id": 42, "username": "legacy"})
    original_copy2 = teleshield.shutil.copy2
    calls = {"count": 0}

    def fail_first_copy(source, destination, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("copy failed")
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(teleshield.shutil, "copy2", fail_first_copy)
    with pytest.raises(RuntimeError, match="遷移"):
        teleshield.ensure_account_registry()

    assert teleshield.list_accounts() == []
    assert teleshield.SESSION_FILE.exists()
    assert teleshield.CONFIG_FILE.exists()

    monkeypatch.setattr(teleshield.shutil, "copy2", original_copy2)
    assert len(teleshield.ensure_account_registry()) == 1


def test_partial_legacy_registry_without_user_id_is_retried_in_place(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    partial = teleshield.create_account("partial-account")
    teleshield.SESSION_FILE.write_bytes(b"legacy-session")
    teleshield.CONFIG_FILE.write_text(json.dumps({"api_id": 123, "marker": "legacy"}), encoding="utf-8")

    records = teleshield.ensure_account_registry()

    assert [item["id"] for item in records] == [partial["id"]]
    assert teleshield.account_store(partial["id"]).config_file.read_text(encoding="utf-8").find("legacy") >= 0
    assert not teleshield.SESSION_FILE.exists()


def test_auto_start_account_is_persisted_and_cleared_on_remove(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    first = teleshield.create_account("account-a")
    second = teleshield.create_account("account-b")
    legacy_cfg = teleshield.load_config(first["id"])
    legacy_cfg["auto_start_protection"] = True
    teleshield.save_config(legacy_cfg, first["id"])

    assert teleshield.get_auto_start_account_id() == first["id"]
    assert teleshield.set_auto_start_account(None) is None
    assert teleshield.get_auto_start_account_id() is None
    assert teleshield.set_auto_start_account(second["id"]) == second["id"]
    assert teleshield.get_auto_start_account_id() == second["id"]
    assert first["auto_start_protection"] is False

    assert teleshield.remove_account(second["id"], delete_files=True) is True
    assert teleshield.get_auto_start_account_id() is None


def test_concurrent_identity_updates_reject_duplicate_user_id(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    barrier = threading.Barrier(2)
    me = SimpleNamespace(id=777, username="same", first_name="Same", last_name="User")

    def update(account_id):
        barrier.wait()
        try:
            teleshield.update_account_identity(account_id, me, "+886900000000")
            return "ok"
        except ValueError:
            return "duplicate"

    results = []
    workers = [threading.Thread(target=lambda account_id=account_id: results.append(update(account_id))) for account_id in ("account-a", "account-b")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(results) == ["duplicate", "ok"]
    assert sum(item.get("user_id") == 777 for item in teleshield.list_accounts()) == 1


def test_cli_main_runs_legacy_migration_before_status(monkeypatch, tmp_path, capsys):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"legacy-session")
    teleshield.save_config({"user_id": 42, "username": "legacy", "blocked_count": 0})
    monkeypatch.setattr(teleshield.sys, "argv", ["teleshield.py", "--status"])

    asyncio.run(teleshield.main())

    assert not teleshield.SESSION_FILE.exists()
    assert len(teleshield.list_accounts()) == 1
    assert "ID: 42" in capsys.readouterr().out


def test_cross_process_identity_updates_reject_one_duplicate(tmp_path):
    teleshield.create_account("account-a", root=tmp_path)
    teleshield.create_account("account-b", root=tmp_path)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_identity_update_process,
            args=(str(tmp_path), account_id, start_event, results),
        )
        for account_id in ("account-a", "account-b")
    ]
    for worker in workers:
        worker.start()
    start_event.set()
    outcomes = [results.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0

    assert sorted(outcomes) == ["duplicate", "ok"]
    assert sum(item.get("user_id") == 9090 for item in teleshield.list_accounts(tmp_path)) == 1


def test_connect_failure_still_hardens_new_session_file(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    teleshield.save_config({"api_id": 123, "api_hash": "[REDACTED]"}, "account-a")

    class FakeClient:
        def __init__(self, session, *args):
            self.session = Path(session)

        async def connect(self):
            self.session.write_bytes(b"partially-created-session")
            self.session.chmod(0o644)
            raise OSError("connect failed")

        async def disconnect(self):
            pass

    with patch.object(telethon, "TelegramClient", FakeClient):
        with pytest.raises(OSError, match="connect failed"):
            asyncio.run(teleshield.discover_managed_groups("account-a"))

    assert store.session_file.stat().st_mode & 0o777 == 0o600


def test_account_store_permission_failure_is_not_ignored(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    store.session_file.write_bytes(b"session")
    original_chmod = Path.chmod

    def deny_session_chmod(path, mode, *args, **kwargs):
        if path == store.session_file:
            raise PermissionError("chmod denied")
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", deny_session_chmod)
    with pytest.raises(PermissionError, match="chmod denied"):
        store.ensure()


def test_registry_permission_failure_is_not_ignored(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    original_chmod = Path.chmod

    def deny_registry_chmod(path, mode, *args, **kwargs):
        if path == tmp_path / "accounts.json":
            raise PermissionError("registry chmod denied")
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", deny_registry_chmod)
    with pytest.raises(PermissionError, match="registry chmod denied"):
        teleshield.set_active_account("account-a")


def test_config_permission_failure_is_not_ignored(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    original_chmod = Path.chmod

    def deny_config_chmod(path, mode, *args, **kwargs):
        if path == store.config_file:
            raise PermissionError("config chmod denied")
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", deny_config_chmod)
    with pytest.raises(PermissionError, match="config chmod denied"):
        teleshield.save_config({"api_id": 123}, "account-a")


def test_config_failure_does_not_commit_registry_identity(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    original_config = store.config_file.read_bytes()

    class FakeClient:
        def __init__(self, session, *args):
            self.session = Path(session)

        async def connect(self):
            self.session.write_bytes(b"new-session")

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=7070, username="new-user", first_name="New")

    def fail_config_write(*args, **kwargs):
        raise OSError("config write failed")

    monkeypatch.setattr(teleshield, "save_config", fail_config_write)
    with patch.object(telethon, "TelegramClient", FakeClient):
        with pytest.raises(OSError, match="config write failed"):
            asyncio.run(
                teleshield.authenticate(
                    "123",
                    "[REDACTED]",
                    "+100****0000",
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    account_id="account-a",
                )
            )

    record = teleshield.get_account("account-a")
    assert record is not None
    assert record.get("user_id") is None
    assert store.config_file.read_bytes() == original_config
    assert not store.session_file.exists()


def test_failed_session_cleanup_is_loud_and_scrubs_credentials(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    teleshield.create_account("account-b")
    teleshield.update_account_identity(
        "account-a",
        SimpleNamespace(id=8080, username="existing", first_name="Existing"),
    )
    store = teleshield.account_store("account-b")

    class FakeClient:
        def __init__(self, session, *args):
            self.session = Path(session)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            self.session.write_bytes(b"authorized-session")
            return SimpleNamespace(id=8080, username="existing", first_name="Existing")

    original_unlink = Path.unlink

    def deny_session_unlink(path, *args, **kwargs):
        if path == store.session_file:
            raise PermissionError("unlink denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_session_unlink)
    with patch.object(telethon, "TelegramClient", FakeClient):
        with pytest.raises(RuntimeError, match="Session.*清理"):
            asyncio.run(
                teleshield.authenticate(
                    "123",
                    "[REDACTED]",
                    "+100****0000",
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    lambda: asyncio.sleep(0, result="[REDACTED]"),
                    account_id="account-b",
                )
            )

    assert store.session_file.read_bytes() == b""
    assert store.session_file.stat().st_mode & 0o777 == 0o600
    record = teleshield.get_account("account-b")
    assert record is not None
    assert record.get("user_id") is None


def test_legacy_sources_are_hidden_before_registry_commit(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"legacy-session")
    teleshield.save_config({"user_id": 42, "username": "legacy"})
    legacy_paths = {teleshield.SESSION_FILE, teleshield.CONFIG_FILE}
    original_unlink = Path.unlink

    def deny_direct_legacy_unlink(path, *args, **kwargs):
        if path in legacy_paths:
            raise PermissionError("legacy unlink denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_direct_legacy_unlink)
    records = teleshield.ensure_account_registry()

    assert len(records) == 1
    assert not teleshield.SESSION_FILE.exists()
    assert not teleshield.CONFIG_FILE.exists()


def test_registry_commit_failure_restores_legacy_layout(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.SESSION_FILE.write_bytes(b"legacy-session")
    teleshield.save_config({"user_id": 42, "username": "legacy"})
    original_write = teleshield._write_account_registry

    def write_then_fail(data, root=None):
        original_write(data, root)
        raise OSError("registry commit failed")

    monkeypatch.setattr(teleshield, "_write_account_registry", write_then_fail)
    with pytest.raises(RuntimeError, match="遷移"):
        teleshield.ensure_account_registry()

    assert teleshield.SESSION_FILE.read_bytes() == b"legacy-session"
    assert json.loads(teleshield.CONFIG_FILE.read_text(encoding="utf-8"))["user_id"] == 42
    assert list((tmp_path / "accounts").glob("*")) == []


def test_running_listener_blocks_local_session_deletion(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    store = teleshield.account_store("account-a")
    store.session_file.write_bytes(b"live-session")
    teleshield.save_config({"api_id": 123, "api_hash": "[REDACTED]"}, "account-a")
    ready = threading.Event()
    release = threading.Event()

    class FakeClient:
        def __init__(self, *args):
            pass

        def on(self, *args, **kwargs):
            return lambda handler: handler

        async def connect(self):
            ready.set()

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def run_until_disconnected(self):
            await asyncio.to_thread(release.wait, 10)

    result = []

    def run_listener():
        with patch.object(telethon, "TelegramClient", FakeClient):
            result.append(asyncio.run(teleshield.listen(account_id="account-a")))

    worker = threading.Thread(target=run_listener)
    worker.start()
    assert ready.wait(10)
    try:
        with pytest.raises(RuntimeError, match="使用中"):
            teleshield.remove_account("account-a")
        with pytest.raises(RuntimeError, match="使用中"):
            teleshield.clear_local_session(account_id="account-a")
        assert store.session_file.read_bytes() == b"live-session"
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()


def test_management_dialog_serializes_account_operations(monkeypatch, tmp_path):
    configure_temp_storage(monkeypatch, tmp_path)
    teleshield.create_account("account-a")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    qt_widgets = pytest.importorskip("PySide6.QtWidgets")
    desktop_app = pytest.importorskip("desktop_app")
    QApplication = qt_widgets.QApplication

    app = QApplication.instance() or QApplication([])
    dialog = desktop_app.ManagementDialog(network_enabled=True, account_id="account-a")

    class RunningWorker:
        @staticmethod
        def isRunning():
            return True

    warnings = []
    monkeypatch.setattr(
        desktop_app.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    dialog.group_worker = RunningWorker()

    assert dialog._operation_available() is False
    assert len(warnings) == 1

    dialog.group_worker = None
    dialog.close()
    app.processEvents()