"""cloudsync.py 測試 — **全部離線**。

原則：
- 絕不真的發出 HTTP：autouse fixture `no_network` 把預設傳輸換成會炸的假函式，
  任何忘記注入假 http 的測試都會立刻失敗（而不是偷偷連上 Cloudflare）。
- 絕不讀真實 .env：autouse fixture `no_dotenv` 把 `config.load_dotenv()` 換成 no-op。
- 憑證測試一律用 monkeypatch 操作環境變數，並驗證 token 不會外流。
"""

import dataclasses
import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

import teleshield.config as config
from teleshield import cloudsync
from teleshield.cloudsync import (
    CloudError,
    CloudListSync,
    SyncResult,
    cloud_sync,
)

ACC = "acct-1234"
NS = "ns-5678"
TOKEN = "cf-token-SECRET-9f3a7d"  # 假 token：任何輸出含它就代表洩漏
ENDPOINT = cloudsync.DEFAULT_ENDPOINT
KEY_W = cloudsync.KV_KEY["whitelist"]
KEY_B = cloudsync.KV_KEY["blacklist"]
VARS = (cloudsync.ENV_ACCOUNT_ID, cloudsync.ENV_NAMESPACE_ID, cloudsync.ENV_API_TOKEN)


# ──────────── fixtures / 假 HTTP ────────────


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """每個測試用獨立的 TELESHIELD_HOME（備份檔也落在 tmp_path）。"""
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "BLOCK_LOG", tmp_path / "block_log.json")
    monkeypatch.setattr(config, "LEARNED_FILE", tmp_path / "learned_patterns.json")
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "user.session")
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """離線保證：預設 HTTP 傳輸一旦被用到就炸。"""

    def bomb(*args, **kwargs):
        raise AssertionError("測試不得真的發出 HTTP 請求（請注入假 http）")

    monkeypatch.setattr(cloudsync, "_urllib_http", bomb)


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """不讀真實 .env（機器上的 ~/.teleshield/.env 可能真有憑證）。"""
    monkeypatch.setattr(config, "load_dotenv", lambda: None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """先清掉三個環境變數，避免受開發機環境影響。"""
    for var in VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def env(monkeypatch):
    """一組可用的假憑證。"""
    monkeypatch.setenv(cloudsync.ENV_ACCOUNT_ID, ACC)
    monkeypatch.setenv(cloudsync.ENV_NAMESPACE_ID, NS)
    monkeypatch.setenv(cloudsync.ENV_API_TOKEN, TOKEN)
    return {"account_id": ACC, "namespace_id": NS, "token": TOKEN}


class FakeKV:
    """有狀態的假 Cloudflare KV：記錄每次呼叫，並真的存值。"""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=dict(headers), body=body, timeout=timeout))
        if "/keys" in url:
            names = [{"name": k} for k in sorted(self.store)]
            return 200, json.dumps({"result": names, "success": True}).encode("utf-8")
        key = unquote(url.split("/values/", 1)[1])
        if method == "GET":
            return (200, self.store[key]) if key in self.store else (404, b"")
        if method == "PUT":
            self.store[key] = body
            return 200, b'{"success": true}'
        if method == "DELETE":
            if key in self.store:
                del self.store[key]
                return 200, b'{"success": true}'
            return 404, b'{"success": false}'
        raise AssertionError(f"未預期的 HTTP 方法：{method}")

    def users(self, key):
        """取出遠端存的名單（轉回 dict）。"""
        return json.loads(self.store[key].decode("utf-8"))["users"]


class ScriptedHTTP:
    """依序回傳腳本化回應；用於錯誤路徑測試。"""

    def __init__(self, *responses, default=(200, b"{}")):
        self.responses = list(responses)
        self.default = default
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=dict(headers), body=body, timeout=timeout))
        return self.responses.pop(0) if self.responses else self.default


class RaisingHTTP:
    """模擬網路層直接拋異常。"""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=dict(headers), body=body, timeout=timeout))
        raise self.exc


def envelope(users, list_type="whitelist"):
    """構造 KV 裡的信封格式。"""
    return json.dumps({"version": 1, "type": list_type, "count": len(users), "users": users},
                      ensure_ascii=False).encode("utf-8")


def set_local(list_type, users):
    """設定本地名單（模擬 manage_list 寫入的形狀）。"""
    cfg = config.load_config()
    cfg[list_type] = users
    config.save_config(cfg)


def entry(name="spammer"):
    return {"added": "2026-01-01", "username": name, "reason": "manual"}


# ──────────── 憑證 ────────────


class TestCredentials:
    def test_all_present(self, env):
        assert cloudsync.credentials() == {"account_id": ACC, "namespace_id": NS, "token": TOKEN}

    def test_missing_all_names_every_variable(self):
        with pytest.raises(CloudError) as exc:
            cloudsync.credentials()
        message = str(exc.value)
        assert cloudsync.ENV_ACCOUNT_ID in message
        assert cloudsync.ENV_NAMESPACE_ID in message
        assert cloudsync.ENV_API_TOKEN in message

    def test_missing_one_names_only_that_variable(self, monkeypatch):
        monkeypatch.setenv(cloudsync.ENV_ACCOUNT_ID, ACC)
        monkeypatch.setenv(cloudsync.ENV_API_TOKEN, TOKEN)
        with pytest.raises(CloudError) as exc:
            cloudsync.credentials()
        message = str(exc.value)
        assert cloudsync.ENV_NAMESPACE_ID in message
        assert cloudsync.ENV_ACCOUNT_ID not in message

    def test_error_never_contains_token_value(self, monkeypatch):
        # 有 token 但缺 account_id：訊息只准講變數名，不准出現 token 值
        monkeypatch.setenv(cloudsync.ENV_API_TOKEN, TOKEN)
        with pytest.raises(CloudError) as exc:
            cloudsync.credentials()
        assert TOKEN not in str(exc.value)

    def test_blank_values_are_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv(cloudsync.ENV_ACCOUNT_ID, "   ")
        monkeypatch.setenv(cloudsync.ENV_NAMESPACE_ID, NS)
        monkeypatch.setenv(cloudsync.ENV_API_TOKEN, TOKEN)
        with pytest.raises(CloudError) as exc:
            cloudsync.credentials()
        assert cloudsync.ENV_ACCOUNT_ID in str(exc.value)


class TestLazyCredentials:
    def test_init_without_credentials_does_not_raise(self):
        CloudListSync()  # 建構時不炸
        CloudListSync(http=FakeKV())

    def test_first_call_without_credentials_raises_and_names_variable(self):
        sync = CloudListSync(http=FakeKV())
        with pytest.raises(CloudError) as exc:
            sync.fetch_remote("whitelist")
        assert cloudsync.ENV_ACCOUNT_ID in str(exc.value)

    def test_explicit_credentials_override_env(self, env):
        kv = FakeKV({KEY_W: envelope({"1": {}})})
        sync = CloudListSync(account_id="other-acc", namespace_id="other-ns", token="other-token", http=kv)
        assert sync.fetch_remote("whitelist") == {"1": {}}
        assert "other-acc" in kv.calls[0].url
        assert kv.calls[0].headers["Authorization"] == "Bearer other-token"


# ──────────── HTTP 連線細節 ────────────


class TestHTTPWiring:
    def test_get_method_url_and_bearer_header(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        CloudListSync(http=kv).fetch_remote("whitelist")
        call = kv.calls[0]
        assert call.method == "GET"
        assert call.url == f"{ENDPOINT}/accounts/{ACC}/storage/kv/namespaces/{NS}/values/teleshield:whitelist"
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        assert call.body is None
        assert call.timeout == 10.0

    def test_put_method_url_content_type(self, env):
        kv = FakeKV({KEY_B: envelope({}, "blacklist")})
        set_local("blacklist", {"42": entry()})
        CloudListSync(http=kv).push("blacklist")
        send = [c for c in kv.calls if c.method == "PUT"][0]
        assert send.url == f"{ENDPOINT}/accounts/{ACC}/storage/kv/namespaces/{NS}/values/teleshield:blacklist"
        assert send.headers["Content-Type"] == "text/plain"
        assert send.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(send.body.decode("utf-8"))["users"].keys() == {"42"}

    def test_delete_uses_delete_method(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        assert CloudListSync(http=kv).clear_remote("whitelist") == 1
        delete = [c for c in kv.calls if c.method == "DELETE"][0]
        assert delete.url.endswith("/values/teleshield:whitelist")
        assert KEY_W not in kv.store

    def test_list_keys_request(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()}), KEY_B: envelope({}, "blacklist")})
        keys = CloudListSync(http=kv).list_keys()
        assert keys == sorted([KEY_W, KEY_B])
        assert "/accounts/{}/storage/kv/namespaces/{}/keys".format(ACC, NS) in kv.calls[0].url
        assert "prefix=teleshield:" in kv.calls[0].url

    def test_custom_endpoint_and_timeout(self, env):
        kv = FakeKV({KEY_W: envelope({})})
        CloudListSync(endpoint="https://proxy.local/api/", timeout=3.5, http=kv).fetch_remote("whitelist")
        assert kv.calls[0].url.startswith("https://proxy.local/api/accounts/")
        assert kv.calls[0].timeout == 3.5


# ──────────── 遠端錯誤處理 ────────────


class TestRemoteErrors:
    def test_403_raises_with_status_code(self, env):
        with pytest.raises(CloudError) as exc:
            CloudListSync(http=ScriptedHTTP((403, b'{"success":false}'))).fetch_remote("whitelist")
        assert "403" in str(exc.value)

    def test_500_on_push_raises_with_status_code(self, env):
        http = ScriptedHTTP((200, envelope({"1": entry()})), (500, b"boom"))
        set_local("whitelist", {"1": entry(), "2": entry()})
        with pytest.raises(CloudError) as exc:
            CloudListSync(http=http).push("whitelist")
        assert "500" in str(exc.value)

    def test_error_message_scrubs_token(self, env):
        # 遠端若把 token 回顯在錯誤內容裡，也不准流出去
        body = json.dumps({"success": False, "errors": [{"message": f"invalid token {TOKEN}"}]}).encode()
        with pytest.raises(CloudError) as exc:
            CloudListSync(http=ScriptedHTTP((403, body))).fetch_remote("whitelist")
        message = str(exc.value)
        assert TOKEN not in message
        assert "***" in message

    def test_network_exception_becomes_cloud_error(self, env):
        with pytest.raises(CloudError) as exc:
            CloudListSync(http=RaisingHTTP(TimeoutError("timed out"))).fetch_remote("whitelist")
        message = str(exc.value)
        assert "TimeoutError" in message
        assert TOKEN not in message

    def test_unknown_list_type_raises(self, env):
        with pytest.raises(CloudError):
            CloudListSync(http=FakeKV()).fetch_remote("greylist")

    def test_404_returns_empty_dict(self, env):
        assert CloudListSync(http=FakeKV()).fetch_remote("whitelist") == {}

    def test_empty_body_returns_empty_dict(self, env):
        assert CloudListSync(http=ScriptedHTTP((200, b""))).fetch_remote("whitelist") == {}

    def test_bad_json_returns_empty_dict(self, env):
        assert CloudListSync(http=ScriptedHTTP((200, b"{not json"))).fetch_remote("whitelist") == {}

    def test_bad_json_in_list_keys_returns_empty_list(self, env):
        assert CloudListSync(http=ScriptedHTTP((200, b"<<<"))).list_keys() == []

    def test_list_keys_tolerates_missing_result(self, env):
        assert CloudListSync(http=ScriptedHTTP((200, b'{"success": true}'))).list_keys() == []

    def test_accepts_bare_mapping_and_id_list(self, env):
        bare = ScriptedHTTP((200, json.dumps({"123": {"added": "x"}}).encode()))
        assert CloudListSync(http=bare).fetch_remote("whitelist") == {"123": {"added": "x"}}
        listed = ScriptedHTTP((200, json.dumps({"users": ["7", "8"]}).encode()))
        assert CloudListSync(http=listed).fetch_remote("whitelist") == {"7": {}, "8": {}}

    def test_non_dict_values_are_normalized(self, env):
        http = ScriptedHTTP((200, json.dumps({"users": {"1": "not-a-dict", "2": {"name": "x"}}}).encode()))
        assert CloudListSync(http=http).fetch_remote("whitelist") == {"1": {}, "2": {"name": "x"}}


# ──────────── push ────────────


class TestPush:
    def test_overwrite_payload_and_counts(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry(), "2": entry()})})
        set_local("whitelist", {"2": entry("本地"), "3": entry()})
        result = CloudListSync(http=kv).push("whitelist")
        assert kv.users(KEY_W).keys() == {"2", "3"}          # 遠端被整份覆蓋
        assert kv.users(KEY_W)["2"]["username"] == "本地"
        assert (result.pushed, result.pulled, result.added, result.removed) == (2, 2, 1, 1)
        assert result.list_type == "whitelist"
        assert "覆蓋" in result.detail

    def test_backup_written_before_overwrite(self, env, isolated_home):
        kv = FakeKV({KEY_W: envelope({"1": entry("舊"), "2": entry("舊")})})
        set_local("whitelist", {"9": entry()})
        CloudListSync(http=kv).push("whitelist")

        backup_file = isolated_home / "cloud_backup_whitelist.json"
        assert backup_file.exists()
        backup = json.loads(backup_file.read_text(encoding="utf-8"))
        assert backup["data"].keys() == {"1", "2"}           # 備份是覆蓋前的雲端舊值
        assert backup["data"]["1"]["username"] == "舊"
        assert backup["source"] == "remote"
        assert backup["list_type"] == "whitelist"
        assert backup["count"] == 2
        assert backup["saved_at"]

    def test_backup_file_permissions_600(self, env, isolated_home):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        CloudListSync(http=kv).push("whitelist")
        mode = (isolated_home / "cloud_backup_whitelist.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_merge_union_local_wins(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry("遠端"), "2": entry("遠端")})})
        set_local("whitelist", {"2": entry("本地"), "3": entry()})
        result = CloudListSync(http=kv).push("whitelist", merge=True)
        users = kv.users(KEY_W)
        assert users.keys() == {"1", "2", "3"}                # 聯集
        assert users["2"]["username"] == "本地"                # 衝突以本地為準
        assert (result.pushed, result.added, result.removed) == (3, 1, 0)

    def test_merge_does_not_drop_remote_only_entries(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        set_local("whitelist", {})
        CloudListSync(http=kv).push("whitelist", merge=True)
        assert set(kv.users(KEY_W)) == {"1"}

    def test_payload_and_backup_contain_no_token(self, env, isolated_home):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        set_local("whitelist", {"2": entry()})
        CloudListSync(http=kv).push("whitelist")
        assert TOKEN.encode() not in kv.store[KEY_W]
        assert TOKEN not in (isolated_home / "cloud_backup_whitelist.json").read_text(encoding="utf-8")
        assert TOKEN not in kv.calls[0].url
        assert TOKEN not in (isolated_home / "config.json").read_text(encoding="utf-8")

    def test_empty_local_over_nonempty_remote_warns(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry(), "2": entry()})})
        set_local("whitelist", {})
        result = CloudListSync(http=kv).push("whitelist")
        assert kv.users(KEY_W) == {}
        assert "⚠️" in result.detail
        assert result.removed == 2

    def test_push_creates_kv_key_when_absent(self, env):
        kv = FakeKV()
        set_local("whitelist", {"5": entry()})
        result = CloudListSync(http=kv).push("whitelist")
        assert kv.users(KEY_W).keys() == {"5"}
        assert (result.pushed, result.pulled, result.added, result.removed) == (1, 0, 1, 0)


# ──────────── pull ────────────


class TestPull:
    def test_overwrite_replaces_local(self, env, isolated_home):
        kv = FakeKV({KEY_W: envelope({"1": entry("雲端")})})
        set_local("whitelist", {"2": entry("本地"), "3": entry("本地")})
        result = CloudListSync(http=kv).pull("whitelist")
        cfg = config.load_config()
        assert cfg["whitelist"].keys() == {"1"}
        assert cfg["whitelist"]["1"]["username"] == "雲端"
        assert (result.pushed, result.pulled, result.added, result.removed) == (0, 1, 1, 2)

    def test_backup_holds_pre_overwrite_local(self, env, isolated_home):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        set_local("whitelist", {"2": entry("本地"), "3": entry("本地")})
        CloudListSync(http=kv).pull("whitelist")
        backup = json.loads((isolated_home / "cloud_backup_whitelist.json").read_text(encoding="utf-8"))
        assert backup["source"] == "local"
        assert backup["data"].keys() == {"2", "3"}

    def test_restore_backup_rolls_back(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        set_local("whitelist", {"2": entry("本地")})
        sync = CloudListSync(http=kv)
        sync.pull("whitelist")
        assert config.load_config()["whitelist"].keys() == {"1"}

        assert cloudsync.restore_backup("whitelist") == 1
        assert config.load_config()["whitelist"].keys() == {"2"}
        assert config.load_config()["whitelist"]["2"]["username"] == "本地"

    def test_restore_backup_without_file_raises(self):
        with pytest.raises(CloudError):
            cloudsync.restore_backup("whitelist")

    def test_load_backup_missing_returns_none(self):
        assert cloudsync.load_backup("whitelist") is None
        cloudsync.backup_path("whitelist").write_text("{broken", encoding="utf-8")
        assert cloudsync.load_backup("whitelist") is None

    def test_merge_local_wins_and_reports_conflicts(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry("遠端"), "2": entry("遠端")})})
        set_local("whitelist", {"1": entry("本地"), "9": entry()})
        result = CloudListSync(http=kv).pull("whitelist", merge=True)
        cfg = config.load_config()
        assert cfg["whitelist"].keys() == {"1", "2", "9"}     # 聯集
        assert cfg["whitelist"]["1"]["username"] == "本地"     # 衝突本地優先
        assert "1" in result.detail and "衝突" in result.detail
        assert result.removed == 0

    def test_merge_ignores_equal_values(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry("同")})})
        set_local("whitelist", {"1": entry("同")})
        result = CloudListSync(http=kv).pull("whitelist", merge=True)
        assert "0 筆衝突" in result.detail

    def test_empty_remote_clears_local_with_warning(self, env):
        kv = FakeKV()
        set_local("whitelist", {"1": entry()})
        result = CloudListSync(http=kv).pull("whitelist")
        assert config.load_config()["whitelist"] == {}
        assert "⚠️" in result.detail
        assert result.removed == 1

    def test_pull_preserves_other_config_keys(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        config.save_config({"api_id": 999, "blacklist": {"7": {}}})
        CloudListSync(http=kv).pull("whitelist")
        cfg = config.load_config()
        assert cfg["api_id"] == 999 and cfg["blacklist"] == {"7": {}}


# ──────────── sync ────────────


class TestSync:
    def test_two_way_merge_keeps_both_sides(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry("遠端")})})
        set_local("whitelist", {"2": entry("本地")})
        result = CloudListSync(http=kv).sync("whitelist")
        assert set(kv.users(KEY_W)) == {"1", "2"}             # 推上雲端
        assert set(config.load_config()["whitelist"]) == {"1", "2"}  # 也拉回本地
        assert result.list_type == "whitelist"
        assert result.pulled == 1 and result.pushed == 2
        assert "雙向合併" in result.detail

    def test_conflict_local_wins_in_both_places(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry("遠端")})})
        set_local("whitelist", {"1": entry("本地")})
        CloudListSync(http=kv).sync("whitelist")
        assert kv.users(KEY_W)["1"]["username"] == "本地"
        assert config.load_config()["whitelist"]["1"]["username"] == "本地"

    def test_sync_writes_remote_backup(self, env, isolated_home):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        CloudListSync(http=kv).sync("whitelist")
        assert (isolated_home / "cloud_backup_whitelist.json").exists()


# ──────────── status ────────────


class TestStatus:
    def test_unconfigured_returns_false_without_raising(self):
        status = CloudListSync(http=FakeKV()).status()
        assert status["configured"] is False
        assert status["remote"] == {"whitelist": None, "blacklist": None}
        assert status["local"] == {"whitelist": 0, "blacklist": 0}

    def test_configured_counts_both_sides(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry(), "2": entry()}), KEY_B: envelope({"9": entry()}, "blacklist")})
        set_local("blacklist", {"7": entry(), "8": entry()})
        status = CloudListSync(http=kv).status()
        assert status["configured"] is True
        assert status["remote"] == {"whitelist": 2, "blacklist": 1}
        assert status["local"] == {"whitelist": 0, "blacklist": 2}

    def test_remote_failure_marks_none(self, env):
        http = ScriptedHTTP((500, b"boom"), (200, envelope({"1": entry()}, "blacklist")))
        status = CloudListSync(http=http).status()
        assert status["configured"] is True
        assert status["remote"] == {"whitelist": None, "blacklist": 1}

    def test_status_makes_no_network_call_when_unconfigured(self):
        kv = FakeKV()
        CloudListSync(http=kv).status()
        assert kv.calls == []


# ──────────── 高階入口 ────────────


class TestCloudSyncEntry:
    def test_all_returns_two_results(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()}), KEY_B: envelope({}, "blacklist")})
        results = cloud_sync("all", http=kv)
        assert len(results) == 2
        assert all(isinstance(r, SyncResult) for r in results)
        assert [r.list_type for r in results] == ["whitelist", "blacklist"]

    def test_single_list_returns_one_result(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        results = cloud_sync("whitelist", action="pull", http=kv)
        assert [r.list_type for r in results] == ["whitelist"]
        assert config.load_config()["whitelist"].keys() == {"1"}

    def test_action_push_passes_merge(self, env):
        kv = FakeKV({KEY_W: envelope({"1": entry()})})
        set_local("whitelist", {"2": entry()})
        [result] = cloud_sync("whitelist", action="push", merge=True, http=kv)
        assert result.pushed == 2 and set(kv.users(KEY_W)) == {"1", "2"}

    def test_invalid_list_type_raises(self, env):
        with pytest.raises(CloudError):
            cloud_sync("greylist", http=FakeKV())

    def test_invalid_action_raises(self, env):
        with pytest.raises(CloudError):
            cloud_sync("whitelist", action="destroy", http=FakeKV())

    def test_missing_credentials_names_variable(self):
        with pytest.raises(CloudError) as exc:
            cloud_sync("all", http=FakeKV())
        assert cloudsync.ENV_ACCOUNT_ID in str(exc.value)

    def test_calls_load_dotenv(self, monkeypatch):
        called = []
        monkeypatch.setattr(config, "load_dotenv", lambda: called.append(True))
        monkeypatch.setenv(cloudsync.ENV_ACCOUNT_ID, ACC)
        monkeypatch.setenv(cloudsync.ENV_NAMESPACE_ID, NS)
        monkeypatch.setenv(cloudsync.ENV_API_TOKEN, TOKEN)
        cloud_sync("whitelist", http=FakeKV())
        assert called == [True]


# ──────────── 其他 ────────────


class TestModulesShape:
    def test_constants(self):
        assert cloudsync.DEFAULT_ENDPOINT == "https://api.cloudflare.com/client/v4"
        assert cloudsync.LIST_TYPES == ("whitelist", "blacklist")
        assert cloudsync.KV_KEY == {"whitelist": "teleshield:whitelist", "blacklist": "teleshield:blacklist"}
        assert cloudsync.backup_path("whitelist").name == "cloud_backup_whitelist.json"

    def test_sync_result_is_frozen(self):
        result = SyncResult(1, 2, 3, 4, "whitelist")
        assert result.detail == ""
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.pushed = 99

    def test_cloud_error_is_runtime_error(self):
        assert issubclass(CloudError, RuntimeError)
