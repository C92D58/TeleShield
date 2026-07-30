import asyncio
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from unittest.mock import patch

import telethon
import teleshield
from desktop_platform import application_command
from telethon.tl.types import Channel, User


def configure_temp_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(teleshield, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(teleshield, "SESSION_FILE", tmp_path / "user.session")
    monkeypatch.setattr(teleshield, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(teleshield, "BLOCK_LOG", tmp_path / "block_log.json")


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
