import asyncio
from datetime import datetime, timezone
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
                "hash",
                "+10000000000",
                lambda: asyncio.sleep(0, result="12345"),
                lambda: asyncio.sleep(0, result="password"),
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
        "api_hash": "hash",
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
        "api_hash": "hash",
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
        "api_hash": "hash",
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
    teleshield.save_config({"api_id": 1234, "api_hash": "hash"})
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
