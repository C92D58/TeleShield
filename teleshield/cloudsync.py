"""可選的雲端名單同步：黑/白名單 ↔ Cloudflare KV。

用途
----
把本機 config.json 內的 `whitelist` / `blacklist` 與 Cloudflare KV 互相同步，
讓多台機器共用同一份名單。

安全設計（重要）
----------------
1. **憑證只從環境變數讀取**（`TELESHIELD_CF_ACCOUNT_ID`、`TELESHIELD_CF_KV_NAMESPACE_ID`、
   `TELESHIELD_CF_API_TOKEN`）。token 只出現在 HTTP 的 `Authorization: Bearer` 標頭；
   錯誤訊息、上傳的 KV 值、備份檔一律不含 token（錯誤訊息再經 `_scrub()` 過濾一次）。
   呼叫端想從 `.env` 載入憑證時，請先自行呼叫 `config.load_dotenv()`（`cloud_sync()` 已代勞）。
2. **破壞性操作先備份**：`push(merge=False)` 先讀取遠端整份名單、`pull(merge=False)` 先讀取
   本地整份名單，兩者都寫入 `<TELESHIELD_HOME>/cloud_backup_<list>.json`，
   可用 `restore_backup()` 回滾。手一滑不會真的把名單弄丟。
3. **零依賴、可離線測試**：只用標準庫；HTTP 傳輸可注入（`http=...` 簽名為
   `http(method, url, headers, body, timeout) -> (status, bytes)`）。

KV 值的格式（`$KEY` = `teleshield:whitelist` / `teleshield:blacklist`）::

    {"version": 1, "type": "whitelist", "updated_at": "...", "count": 2,
     "users": {"12345": {"added": "2026-01-01", "username": "spammer"}}}

讀取端對格式很寬容：`{"users": {...}}` 信封、直接把 id 對到資訊的裸 mapping、
或純 id 列表都能解析；空值／壞 JSON 一律視為空名單（不拋錯）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from . import config

__all__ = [
    "DEFAULT_ENDPOINT",
    "LIST_TYPES",
    "KV_KEY",
    "ENV_ACCOUNT_ID",
    "ENV_NAMESPACE_ID",
    "ENV_API_TOKEN",
    "CloudError",
    "SyncResult",
    "CloudListSync",
    "credentials",
    "cloud_sync",
    "backup_path",
    "load_backup",
    "restore_backup",
]

DEFAULT_ENDPOINT = "https://api.cloudflare.com/client/v4"
LIST_TYPES = ("whitelist", "blacklist")
KV_KEY = {"whitelist": "teleshield:whitelist", "blacklist": "teleshield:blacklist"}

ENV_ACCOUNT_ID = "TELESHIELD_CF_ACCOUNT_ID"
ENV_NAMESPACE_ID = "TELESHIELD_CF_KV_NAMESPACE_ID"
ENV_API_TOKEN = "TELESHIELD_CF_API_TOKEN"

#: 可注入的 HTTP 傳輸簽名：method, url, headers, body, timeout -> (status, raw bytes)
HttpFn = Callable[[str, str, "dict[str, str]", Optional[bytes], float], "tuple[int, bytes]"]


class CloudError(RuntimeError):
    """雲端同步相關錯誤（憑證缺失、HTTP 失敗、名單類型錯誤）。

    保證：訊息中**不會**出現 API token（缺憑證時只講環境變數名稱）。
    """


@dataclass(frozen=True)
class SyncResult:
    """一次同步動作的結果摘要。"""

    pushed: int          # 本次寫到遠端的筆數
    pulled: int          # 本次從遠端讀到的筆數
    added: int           # 本次新增的筆數（相對被寫入的那一端）
    removed: int         # 本次移除的筆數（相對被寫入的那一端）
    list_type: str
    detail: str = ""


# ──────────── 憑證 ────────────


def credentials() -> dict:
    """從環境變數讀取 Cloudflare 憑證。

    回傳 ``{"account_id": ..., "namespace_id": ..., "token": ...}``。
    任一個缺少就 raise `CloudError`，訊息會**逐一列出缺少的環境變數名稱**，
    讓使用者照著修；token 的值永遠不會出現在訊息裡。
    """
    pairs = (
        ("account_id", ENV_ACCOUNT_ID),
        ("namespace_id", ENV_NAMESPACE_ID),
        ("token", ENV_API_TOKEN),
    )
    values = {}
    missing = []
    for field, var in pairs:
        value = (os.getenv(var) or "").strip()
        if value:
            values[field] = value
        else:
            missing.append(var)
    if missing:
        raise CloudError(
            "雲端同步未設定：缺少環境變數 "
            + "、".join(missing)
            + "（請寫進 ~/.teleshield/.env 或項目根 .env，僅本機保存）"
        )
    return values


def _check_list_type(list_type: str) -> str:
    """驗證名單類型，回傳正規化後的值。"""
    if list_type not in LIST_TYPES:
        raise CloudError(f"未知的名單類型：{list_type!r}（可用：{'、'.join(LIST_TYPES)}）")
    return list_type


def _kv_key(list_type: str) -> str:
    return KV_KEY[_check_list_type(list_type)]


# ──────────── 本地名單存取 ────────────


def _load_local(list_type: str) -> dict:
    """讀取本地名單（config.json 內的 whitelist/blacklist）。"""
    cfg = config.load_config()
    raw = cfg.get(_check_list_type(list_type)) or {}
    return _normalize_list(raw)


def _save_local(list_type: str, users: dict) -> None:
    """寫回本地名單（其他 config 欄位保持不變）。"""
    cfg = config.load_config()
    cfg[_check_list_type(list_type)] = users
    config.save_config(cfg)


def _normalize_list(data) -> dict:
    """把任意形狀的名單資料正規化為 ``{user_id 字串: dict}``。

    支援：`{"users": {...}}` 信封、`{"users": [...]}` 列表、裸 id mapping、純 id 列表。
    裸 mapping 只接受全數字鍵（避免把無關的 metadata 當成使用者）；其餘一律當空名單。
    """
    if isinstance(data, dict):
        users = data.get("users")
        if users is None:
            return {str(k): (v if isinstance(v, dict) else {}) for k, v in data.items() if str(k).isdigit()}
    elif isinstance(data, list):
        users = data
    else:
        return {}

    if isinstance(users, list):
        return {str(u): {} for u in users}
    if not isinstance(users, dict):
        return {}
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in users.items()}


def _encode_list(list_type: str, users: dict) -> bytes:
    """序列化為要寫進 KV 的原始值（純 JSON，絕不含憑證）。"""
    payload = {
        "version": 1,
        "type": list_type,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(users),
        "users": users,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# ──────────── 備份（回滾用） ────────────


def backup_path(list_type: str) -> Path:
    """備份檔路徑：``<TELESHIELD_HOME>/cloud_backup_<list_type>.json``。"""
    return config.HOME_DIR / f"cloud_backup_{_check_list_type(list_type)}.json"


def _write_backup(list_type: str, users: dict, *, source: str) -> Path:
    """原子寫入備份檔（權限 600）。

    `source` 標明備份的是哪一端：`"remote"`（push 前的雲端舊值）或 `"local"`（pull 前的本地舊值）。
    """
    path = backup_path(list_type)
    payload = {
        "list_type": list_type,
        "source": source,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "count": len(users),
        "data": users,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    return path


def load_backup(list_type: str) -> Optional[dict]:
    """讀取備份檔；不存在或壞檔時回 `None`。"""
    try:
        return json.loads(backup_path(list_type).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def restore_backup(list_type: str) -> int:
    """把備份檔裡的 `data` 還原回本地名單，回傳還原筆數。

    用於整份覆蓋（push / pull）之後的回滾。找不到可用備份時 raise `CloudError`。
    """
    backup = load_backup(list_type)
    data = backup.get("data") if isinstance(backup, dict) else None
    if not isinstance(data, dict):
        raise CloudError(f"找不到可用的備份檔：{backup_path(list_type)}")
    users = _normalize_list(data)
    _save_local(list_type, users)
    return len(users)


# ──────────── HTTP 傳輸 ────────────


def _urllib_http(method: str, url: str, headers: dict, body, timeout: float):
    """預設傳輸：標準庫 urllib。回傳 `(status, bytes)`，HTTP 錯誤碼不拋例外。"""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 交給上層判斷
        try:
            payload = exc.read()
        except Exception:  # pragma: no cover - 讀不到 body 不影響狀態碼判斷
            payload = b""
        return int(exc.code), payload


# ──────────── 同步主體 ────────────


class CloudListSync:
    """黑/白名單 ↔ Cloudflare KV 同步器。

    建構時**不檢查憑證**（方便 dry-run 與離線測試）；真正要發 HTTP 時才解析憑證，
    缺少時 raise `CloudError`。
    """

    def __init__(
        self,
        *,
        account_id: Optional[str] = None,
        namespace_id: Optional[str] = None,
        token: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 10.0,
        http: Optional[HttpFn] = None,
    ):
        self._account_id = account_id
        self._namespace_id = namespace_id
        self._token = token
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self._timeout = timeout
        self._http = http

    # ── 內部工具 ──

    def _creds(self) -> dict:
        """顯式參數優先，缺的再從環境變數補；仍缺則 raise CloudError。"""
        if self._account_id and self._namespace_id and self._token:
            return {"account_id": self._account_id, "namespace_id": self._namespace_id, "token": self._token}
        env = credentials()
        return {
            "account_id": self._account_id or env["account_id"],
            "namespace_id": self._namespace_id or env["namespace_id"],
            "token": self._token or env["token"],
        }

    def _scrub(self, text: str) -> str:
        """把 token 從任何要外流的字串中抹掉（縱深防禦）。

        token 可能來自建構參數，也可能來自環境變數——兩種都要過濾。
        """
        token = self._token
        if not token:
            try:
                token = self._creds().get("token")
            except CloudError:
                token = None
        if token and token in text:
            return text.replace(token, "***")
        return text

    def _snippet(self, body: bytes, limit: int = 200) -> str:
        """錯誤訊息用的回應片段（已去敏）。"""
        if not body:
            return ""
        text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
        return self._scrub(text[:limit])

    def _request(self, method: str, url: str, *, body: Optional[bytes] = None):
        """發一個請求，回傳 `(status, bytes)`。網路層異常一律轉為 CloudError。"""
        creds = self._creds()
        headers = {"Authorization": f"Bearer {creds['token']}"}
        if body is not None:
            headers["Content-Type"] = "text/plain"
        http = self._http if self._http is not None else _urllib_http
        try:
            status, payload = http(method, url, headers, body, self._timeout)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(
                f"無法連線 Cloudflare（{method} {url}）：{type(exc).__name__}: {self._scrub(str(exc))}"
            ) from exc
        return int(status), payload or b""

    def _values_url(self, key: str) -> str:
        creds = self._creds()
        return (
            f"{self._endpoint}/accounts/{creds['account_id']}"
            f"/storage/kv/namespaces/{creds['namespace_id']}/values/{quote(key, safe=':')}"
        )

    def _keys_url(self, prefix: str, limit: int) -> str:
        creds = self._creds()
        return (
            f"{self._endpoint}/accounts/{creds['account_id']}"
            f"/storage/kv/namespaces/{creds['namespace_id']}"
            f"/keys?limit={int(limit)}&prefix={quote(prefix, safe=':')}"
        )

    # ── 遠端讀取 ──

    def fetch_remote(self, list_type: str) -> dict:
        """讀取遠端名單，回傳 ``{user_id 字串: dict}``。

        - KV 尚無此 key（HTTP 404）→ 回 `{}`
        - 回應為空或不是合法 JSON → 回 `{}`（視為空名單，不拋錯）
        - 其他非 200 → raise `CloudError`
        """
        key = _kv_key(list_type)
        url = self._values_url(key)
        status, body = self._request("GET", url)
        if status == 404:
            return {}
        if status != 200:
            raise CloudError(f"讀取雲端名單失敗：HTTP {status}（GET {url}）{self._snippet(body)}")
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return {}
        return _normalize_list(data)

    def list_keys(self, *, prefix: str = "teleshield:", limit: int = 100) -> list:
        """列出遠端符合前綴的 key 名稱（供除錯／巡檢）。壞 JSON 回 `[]`。"""
        url = self._keys_url(prefix, limit)
        status, body = self._request("GET", url)
        if status != 200:
            raise CloudError(f"列出雲端 key 失敗：HTTP {status}（GET {url}）{self._snippet(body)}")
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return []
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list):
            return []
        return [item["name"] for item in result if isinstance(item, dict) and "name" in item]

    # ── 寫入 ──

    def push(self, list_type: str, *, merge: bool = False) -> SyncResult:
        """把本地名單推上雲端。

        - `merge=False`（預設）：本地整份覆蓋遠端。
        - `merge=True`：遠端 ∪ 本地，同 id 衝突**以本地為準**。

        兩種模式都**先讀取遠端舊值並寫入 `<HOME>/cloud_backup_<list>.json`**，
        這是刻意設計：整份覆蓋是不可逆的，備份讓 `restore_backup()` 能救回來。
        """
        key = _kv_key(list_type)
        remote = self.fetch_remote(list_type)   # 兼作備份來源
        local = _load_local(list_type)
        payload = {**remote, **local} if merge else dict(local)

        backup = _write_backup(list_type, remote, source="remote")

        url = self._values_url(key)
        status, body = self._request("PUT", url, body=_encode_list(list_type, payload))
        if status not in (200, 201):
            raise CloudError(f"推送名單失敗：HTTP {status}（PUT {url}）{self._snippet(body)}")

        added = sum(1 for uid in payload if uid not in remote)
        removed = sum(1 for uid in remote if uid not in payload)
        detail = (
            f"{'合併' if merge else '覆蓋'}推送 {len(payload)} 筆到雲端"
            f"（雲端原有 {len(remote)} 筆；新增 {added}、移除 {removed}）"
        )
        if not payload and remote:
            detail += f"；⚠️ 本地為空，已清空雲端 {len(remote)} 筆，可用 {backup.name} 還原"
        return SyncResult(
            pushed=len(payload), pulled=len(remote), added=added, removed=removed,
            list_type=list_type, detail=detail,
        )

    def pull(self, list_type: str, *, merge: bool = False) -> SyncResult:
        """把雲端名單拉回本地。

        - `merge=False`（預設）：遠端整份覆蓋本地。
        - `merge=True`：本地 ∪ 遠端，同 id 衝突**以本地為準**，衝突筆數記在 `detail`。

        兩種模式都**先備份本地舊值**到 `<HOME>/cloud_backup_<list>.json`（source="local"），
        覆蓋後可用 `restore_backup()` 回滾。
        """
        _kv_key(list_type)
        remote = self.fetch_remote(list_type)
        local = _load_local(list_type)

        backup = _write_backup(list_type, local, source="local")

        if merge:
            conflicts = sum(1 for uid, info in local.items() if uid in remote and remote[uid] != info)
            merged = {**remote, **local}
            detail = f"合併雲端 {len(remote)} 筆到本地（本地優先，{conflicts} 筆衝突以本地為準）"
        else:
            merged = dict(remote)
            detail = f"以雲端 {len(remote)} 筆覆蓋本地 {len(local)} 筆"
            if not remote and local:
                detail += f"；⚠️ 雲端為空，已清空本地 {len(local)} 筆，可用 {backup.name} 還原"

        _save_local(list_type, merged)
        added = sum(1 for uid in merged if uid not in local)
        removed = sum(1 for uid in local if uid not in merged)
        return SyncResult(
            pushed=0, pulled=len(remote), added=added, removed=removed,
            list_type=list_type, detail=detail,
        )

    def sync(self, list_type: str) -> SyncResult:
        """雙向合併：先 `pull(merge=True)` 再 `push(merge=True)`，衝突一律本地優先。

        兩步都以 merge 進行，所以遠端與本地都只增不減（除非本地主動刪除）。
        """
        pulled = self.pull(list_type, merge=True)
        pushed = self.push(list_type, merge=True)
        return SyncResult(
            pushed=pushed.pushed, pulled=pulled.pulled, added=pulled.added, removed=pulled.removed,
            list_type=list_type, detail=f"雙向合併：{pulled.detail}；{pushed.detail}",
        )

    def clear_remote(self, list_type: str) -> int:
        """刪除雲端該 key（先備份遠端舊值），回傳刪掉的筆數。"""
        key = _kv_key(list_type)
        remote = self.fetch_remote(list_type)
        _write_backup(list_type, remote, source="remote")
        url = self._values_url(key)
        status, body = self._request("DELETE", url)
        if status not in (200, 204, 404):
            raise CloudError(f"刪除雲端名單失敗：HTTP {status}（DELETE {url}）{self._snippet(body)}")
        return len(remote)

    # ── 狀態 ──

    def status(self) -> dict:
        """回傳 `{"configured": bool, "remote": {list: n}, "local": {list: n}}`。

        憑證未設定時**不拋錯**，只回 `configured=False`（`remote` 的值為 `None`，代表未知）；
        查詢某一端失敗時該端為 `None`。
        """
        cfg = config.load_config()
        local = {lt: len(_normalize_list(cfg.get(lt) or {})) for lt in LIST_TYPES}
        try:
            self._creds()
        except CloudError:
            return {"configured": False, "remote": dict.fromkeys(LIST_TYPES), "local": local}

        remote = {}
        for lt in LIST_TYPES:
            try:
                remote[lt] = len(self.fetch_remote(lt))
            except CloudError:
                remote[lt] = None
        return {"configured": True, "remote": remote, "local": local}


# ──────────── 高階入口 ────────────


def cloud_sync(
    list_type: str = "all",
    *,
    action: str = "sync",
    merge: bool = False,
    http: Optional[HttpFn] = None,
) -> list:
    """高階入口：對 `list_type`（`"all"` = 黑白名單各一次）執行 `action`。

    - `action="sync"`（預設）：雙向合併（衝突本地優先）
    - `action="push"` / `"pull"`：搭配 `merge` 決定是否合併
    - 會先呼叫 `config.load_dotenv()` 載入 `.env`（已存在的環境變數優先），
      憑證仍缺就直接 raise `CloudError`（訊息指出缺哪個環境變數）。
    - `http` 可注入自訂傳輸（測試／代理用）；不給則走 urllib。
    """
    targets = LIST_TYPES if list_type in ("all", None) else (_check_list_type(list_type),)
    if action not in ("push", "pull", "sync"):
        raise CloudError(f"未知的同步動作：{action!r}（可用：push、pull、sync）")

    config.load_dotenv()
    sync = CloudListSync(http=http)
    sync._creds()  # 憑證缺失時立即失敗，不要等第一筆 HTTP 才炸

    results = []
    for lt in targets:
        if action == "sync":
            results.append(sync.sync(lt))
        else:
            results.append(getattr(sync, action)(lt, merge=merge))
    return results
