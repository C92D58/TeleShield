"""自動更新：查 GitHub Release、比版本、下載驗 sha256、呼叫 pip 安裝。

★ 為什麼整支只用標準庫：
  更新器是「唯一還活著、要負責把其他東西換掉」的那支程式。它一旦依賴第三方套件，
  就會出現「要先更新才能更新」的死結 ✗ 所以 urllib + hashlib 自己來。

★ 為什麼所有網路呼叫都經過可注入的 `fetcher`：
  更新相關的測試一旦真的連 GitHub，CI 會變慢、會 flaky、會被 rate limit 隨機打掛 ✗
  這裡把「連線」縮到單一函式 ✗ 測試注入假的 fetcher ✗ 一個封包都不會出去。

★ 為什麼版本比較自己寫（不吃 packaging）：
  本專案的 tag 只有 `vX.Y.Z` 與 `vX.Y.Z-rcN` 兩種形狀 ✗
  為了「-rc1 要比正式版小」多一個執行期依賴不值得，而且 packaging 不在依賴清單裡。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path

__all__ = [
    "DEFAULT_REPO",
    "RELEASE_API",
    "APPLY_TIMEOUT",
    "UpdateError",
    "Release",
    "parse_version",
    "current_version",
    "fetch_release",
    "check",
    "checksum",
    "verify_checksum",
    "find_asset",
    "download",
    "apply",
]

DEFAULT_REPO = "C92D58/TeleShield"
RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"

APPLY_TIMEOUT = 300            # pip install 的逾時秒數
_CHUNK = 1024 * 1024           # 算 sha256 時的讀取塊
_USER_AGENT = "TeleShield-Updater"

# ★ pre-release 的種類權重：同一個 core 版本下，數字越小越早（也越小）。
#   dev < alpha < beta < rc < 正式版。表上沒有的字（例如 "preview9"）給 5，
#   仍然小於正式版，但排得比 rc 晚。
_PRERELEASE_RANK = {
    "dev": 0,
    "a": 1,
    "alpha": 1,
    "b": 2,
    "beta": 2,
    "c": 3,
    "rc": 4,
    "pre": 4,
    "preview": 4,
}
_PRERELEASE_UNKNOWN = 5
_FINAL_RANK = 99
_VERSION_WIDTH = 5             # core 補零到幾位（"1.2" 與 "1.2.0" 要相等）


class UpdateError(Exception):
    """更新流程任何一步失敗（查詢、下載、校驗、安裝）。

    訊息一律寫成人看得懂的句子 ✗ 帶 HTTP 狀態碼、URL 或檔名 ✗
    CLI 直接把 str(e) 印出來就夠了，不需要另外翻譯。
    """


@dataclass(frozen=True)
class Release:
    """一個 GitHub release 的快照（欄位名對齊 GitHub API 的語意）。"""

    tag: str                    # 原始 tag，例如 "v0.11.0"
    version: str                # 去掉 v 前綴，例如 "0.11.0"
    name: str
    body: str                   # release notes（Markdown）
    html_url: str
    assets: list[dict]          # 原始 assets（name / browser_download_url / size / digest）
    published_at: str           # ISO 8601 字串，不解析成 datetime（少了 dateutil 會踩時區）


# ════════════════════════════════════════════════════════════════════
# 版本比較
# ════════════════════════════════════════════════════════════════════
def parse_version(s: str) -> tuple:
    """把版本字串轉成可直接比大小的 tuple。

    "v0.11.0"    -> (0, 11, 0, 0, 0, 1, 99, 0)
    "0.11.0-rc1" -> (0, 11, 0, 0, 0, 0, 4, 1)

    前 5 位是 core（補零到固定寬度），後 3 位依序是：
    是否正式版（1/0）、pre-release 種類權重、pre-release 序號。

    ★ 為什麼 core 要補零：tuple 比較是逐位比、比完之後短的算小 ✗
      不補零的話 "1.2" < "1.2.0" ✗ 但這兩者其實是同一版 ✗ 會誤報「有新版本」。
    ★ 為什麼正式版要塞 1、pre-release 塞 0：
      只在尾端「多一個元素」判斷 pre-release，Python 會把多的那個判成比較大 ✗
      結果 0.11.0-rc1 > 0.11.0 ✗ 正好反了。所以一定要用同一位置的不同數值。
    ★ 為什麼壞字串不拋錯：tag 是使用者（或 GitHub）給的自由文字 ✗
      更新檢查不該讓 CLI 當掉 ✗ 認不出來的東西一律當成 0.0.0。
    """
    text = str(s or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    core, _, pre = text.partition("-")
    if not pre:
        core, _, pre = text.partition("+")

    numbers = [_leading_int(part) for part in core.split(".")]
    numbers = numbers[:_VERSION_WIDTH] + [0] * (_VERSION_WIDTH - len(numbers))

    if not pre:
        return tuple(numbers) + (1, _FINAL_RANK, 0)

    name_match = re.match(r"([A-Za-z]+)", pre)
    rank = _PRERELEASE_RANK.get(name_match.group(1).lower(), _PRERELEASE_UNKNOWN) if name_match else _PRERELEASE_UNKNOWN
    num_match = re.search(r"(\d+)", pre)
    number = int(num_match.group(1)) if num_match else 0
    return tuple(numbers) + (0, rank, number)


def _leading_int(part: str) -> int:
    """取字串開頭的連續數字（"3rc" -> 3）；沒有數字回 0（"abc" -> 0）。"""
    match = re.match(r"(\d+)", part)
    return int(match.group(1)) if match else 0


def current_version() -> str:
    """目前安裝的版本；取不到回 "0.0.0"。

    ★ 為什麼讀 importlib.metadata 而不是 teleshield.__version__：
      套件裡的 __version__ 是手寫的第二份資料 ✗ 一定會有人忘記同步 ✗
      metadata 是安裝當下由打包工具從 pyproject.toml 寫進去的 ✗ 只有這份可信。
    """
    try:
        return importlib_metadata.version("teleshield")
    except importlib_metadata.PackageNotFoundError:
        return "0.0.0"


# ════════════════════════════════════════════════════════════════════
# 抓 release
# ════════════════════════════════════════════════════════════════════
def _resolve_token(token: str | None = None) -> str | None:
    """決定要用哪個 GitHub token：參數 > GITHUB_TOKEN > TELESHIELD_GITHUB_TOKEN。

    ★ 為什麼 GITHUB_TOKEN 優先：GitHub Actions 會自動注入這個變數 ✗
      release 流程與 CI 不用另外設定就能打 API。私人倉庫另外用專案前綴的變數隔開。
    """
    if token:
        return token
    return os.getenv("GITHUB_TOKEN") or os.getenv("TELESHIELD_GITHUB_TOKEN") or None


def _urlopen_bytes(url: str, timeout: float, token: str | None, *, api: bool = True) -> bytes:
    """預設的網路實作。★ User-Agent 是 GitHub API 的硬性要求 ✗ 沒帶會回 403。"""
    headers = {"User-Agent": _USER_AGENT}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def _http_status_message(err: urllib.error.HTTPError, what: str) -> str:
    """把 HTTPError 翻成看得懂的中文訊息（一定帶狀態碼）。

    ★ 為什麼要特別認 403 / 429：未認證的 GitHub API 每小時只有 60 次 ✗
      撞到 rate limit 時訊息只寫 "Forbidden" ✗ 使用者會以為是 repo 設錯 ✗ 白找半天。
    """
    status = int(getattr(err, "code", 0) or 0)
    headers = getattr(err, "headers", None) or {}
    try:
        remaining = str(headers.get("X-RateLimit-Remaining", "") or "")
    except Exception:  # pragma: no cover - headers 形狀不正常時不必陪葬
        remaining = ""

    if status == 401:
        return "GitHub 認證失敗（HTTP 401）：GITHUB_TOKEN 無效或已過期"
    if status in (403, 429) or remaining == "0":
        return (
            "GitHub 拒絕存取（HTTP %d）✗ 很可能是 rate limit（未認證每小時 60 次）✗ "
            "設 GITHUB_TOKEN 或稍後再試" % status
        )
    if status == 404:
        return "找不到（HTTP 404）：%s ✗ 確認 repo 名稱、tag 是否已發布、資產名稱；私人資源要設 GITHUB_TOKEN" % what
    reason = getattr(err, "reason", "") or "no reason"
    return "HTTP %d（%s）：%s" % (status, reason, what)


def _fetch_bytes(url: str, timeout: float, token: str | None, fetcher, *, api: bool = True) -> bytes:
    """實際發請求：注入的 fetcher 優先，否則用 urllib。失敗一律翻成 UpdateError。

    ★ 為什麼連注入的 fetcher 拋的錯也要翻譯：
      上層（CLI）只需要認一種例外 ✗ 不然它得同時接 HTTPError / URLError / socket.timeout ✗
      漏掉任何一種就是一份使用者看不懂的 traceback。
    """
    try:
        if fetcher is not None:
            raw = fetcher(url, timeout, token)
        else:
            raw = _urlopen_bytes(url, timeout, token, api=api)
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        raise UpdateError(_http_status_message(e, url)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("連線失敗（%s）：%s" % (url, e)) from e

    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        return raw.encode("utf-8")
    raise UpdateError("fetcher 必須回傳 bytes ✗ 拿到 %s" % type(raw).__name__)


def fetch_release(repo: str = DEFAULT_REPO, *, timeout: float = 10.0,
                  token: str | None = None, fetcher=None) -> Release:
    """抓 repo 的最新 release（GitHub API `/releases/latest`）。

    `fetcher(url, timeout, token) -> bytes` 可注入 ✗ 測試一律注入假的 ✗ 不連網。
    失敗 raise UpdateError ✗ 訊息帶 HTTP 狀態碼、URL，rate limit 會明講。
    """
    if not repo or "/" not in str(repo):
        raise UpdateError("repo 必須是 owner/name 形式 ✗ 收到 %r" % (repo,))
    url = RELEASE_API.format(repo=repo)
    resolved = _resolve_token(token)

    try:
        raw = _fetch_bytes(url, timeout, resolved, fetcher)
    except UpdateError as e:
        raise UpdateError("取得 %s 的 release 失敗 ✗ %s" % (repo, e)) from e

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        snippet = bytes(raw[:120]).decode("utf-8", "replace")
        raise UpdateError("GitHub 回應不是合法 JSON（%s）：%s" % (type(e).__name__, snippet)) from e

    if not isinstance(payload, dict):
        raise UpdateError("GitHub 回應不是 JSON 物件 ✗ 拿到 %s" % type(payload).__name__)

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("release 缺少 tag_name ✗ 拿到的欄位：%s" % list(payload)[:8])

    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []

    return Release(
        tag=tag,
        version=tag[1:] if tag[:1] in ("v", "V") else tag,
        name=str(payload.get("name") or tag),
        body=str(payload.get("body") or ""),
        html_url=str(payload.get("html_url") or "https://github.com/%s/releases" % repo),
        assets=[a for a in assets if isinstance(a, dict)],
        published_at=str(payload.get("published_at") or ""),
    )


def check(current: str | None = None, repo: str = DEFAULT_REPO, *, fetcher=None) -> dict:
    """比對目前版本與最新 release。

    回傳 `{"current", "latest", "update_available", "release", "notes"}`；
    current 省略時用 `current_version()`（importlib.metadata）。

    ★ 為什麼「有新版本」用 `>` 而不是 `!=`：
      本機版本可能比 release 還新（開發中的 0.12.0 ✗ 或自己 pip install -e 的版本）✗
      用 != 會叫使用者「降級」✗ 比什麼都糟。
    """
    release = fetch_release(repo, fetcher=fetcher)
    cur = current or current_version()
    return {
        "current": cur,
        "latest": release.version,
        "update_available": parse_version(release.version) > parse_version(cur),
        "release": release,
        "notes": release.body,
    }


# ════════════════════════════════════════════════════════════════════
# 校驗碼
# ════════════════════════════════════════════════════════════════════
def checksum(path: Path) -> str:
    """算檔案的 sha256（小寫十六進位）。分塊讀 ✗ 大檔不會吃光記憶體。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha256(expected: str) -> str:
    """正規化校驗碼：去空白、轉小寫、吃掉 "sha256:" / "sha256=" 前綴。

    ★ GitHub API 的 asset digest 欄位就是 "sha256:xxxx" 的形狀 ✗
      直接拿來比會永遠不相等 ✗ 所以在唯一的入口把它拆掉 ✗ 呼叫端不必知道規矩。
    """
    text = str(expected or "").strip().lower()
    for prefix in ("sha256:", "sha256="):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.replace(" ", "").replace("\t", "")


def verify_checksum(path: Path, expected_sha256: str) -> bool:
    """檔案 sha256 是否等於 expected（大小寫不敏感 ✗ 可帶 "sha256:" 前綴）。

    檔案不存在 raise FileNotFoundError ✗ expected 是空的 raise ValueError。
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError("找不到要校驗的檔案：%s" % target)
    expected = _normalize_sha256(expected_sha256)
    if not expected:
        raise ValueError("expected_sha256 不可為空")
    return checksum(target) == expected


# ════════════════════════════════════════════════════════════════════
# 挑檔、下載
# ════════════════════════════════════════════════════════════════════
def find_asset(release: Release, prefer: str = ".whl") -> dict | None:
    """從 assets 挑一個可安裝的檔：prefer → .whl → .tar.gz ✗ 都沒有回 None。

    ★ 為什麼 .whl 優先於 .tar.gz：
      wheel 是裝好的成品 ✗ pip 解開就好；sdist / code.tar.gz 還要跑建置 ✗
      在使用者機器上缺編譯器就失敗 ✗ 更新器最不該做的就是「換個方式壞掉」。

    回傳的是**原始 asset dict**✗ 直接把它餵給 `download(url=asset["browser_download_url"],
    expected_sha256=asset.get("digest"))` 就好 ✗ 不需要再解析一層。
    """
    assets = [a for a in (release.assets or []) if isinstance(a, dict) and a.get("name")]
    for suffix in dict.fromkeys([str(prefer or "").lower(), ".whl", ".tar.gz"]):
        if not suffix:
            continue
        for asset in assets:
            if str(asset["name"]).lower().endswith(suffix):
                return asset
    return None


def _part_path(dest: Path) -> Path:
    """暫存檔名：<dest>.part（同一目錄 ✗ 改名是同檔案系統的原子操作）。"""
    return dest.with_name(dest.name + ".part")


def _quiet_unlink(path: Path) -> None:
    """刪檔，失敗就算了（清理路徑不該再拋一個新錯蓋掉原本的）。"""
    try:
        path.unlink()
    except OSError:
        pass


def download(url: str, dest: Path, *, expected_sha256: str | None = None,
             timeout: float = 60.0, fetcher=None) -> Path:
    """下載 url 到 dest。先寫 `<dest>.part` ✗ 校驗通過才改名 ✗ 失敗不留半個檔。

    ★ 為什麼一定要先寫 .part：
      直接寫 dest 的話，連線斷掉會留下一個「看起來存在、其實不完整」的檔案 ✗
      下一次 `pip install` 會很開心地把它裝上去 ✗ 而且錯誤訊息會指向十萬八千里外。
    ★ 為什麼 expected_sha256 給了就**必須**驗：
      校驗碼是唯一擋在 GitHub 帳號被盜 / 中間人換檔中間的東西 ✗
      給了卻「驗不過就放行」等於沒有 ✗ 所以驗不過就 raise UpdateError 並把檔案刪掉。
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(target)

    expected = _normalize_sha256(expected_sha256) if expected_sha256 else ""
    if expected_sha256 and not expected:
        raise UpdateError("expected_sha256 格式不正確：%r" % (expected_sha256,))

    try:
        data = _fetch_bytes(url, timeout, _resolve_token(), fetcher, api=False)
        part.write_bytes(data)
        if expected and not verify_checksum(part, expected):
            raise UpdateError(
                "校驗碼不符 ✗ 拒絕安裝 %s：期望 %s ✗ 實際 %s" % (target.name, expected, checksum(part))
            )
        part.replace(target)   # ★ 到這裡才讓檔案「出現」在 dest
    except BaseException:
        _quiet_unlink(part)
        raise
    return target


# ════════════════════════════════════════════════════════════════════
# 安裝
# ════════════════════════════════════════════════════════════════════
def apply(wheel: Path, *, dry_run: bool = False) -> dict:
    """用目前的直譯器跑 `pip install --upgrade <wheel>`。

    回傳 `{"installed", "stdout", "stderr", "command"}`；dry_run=True 只回傳指令不執行。

    ★ 為什麼是 `sys.executable -m pip` 而不是 `pip`：
      一台機器上常常有好幾個 Python ✗ 直接叫 "pip" 會裝進別的 site-packages ✗
      使用者重開 teleshield 還是舊版 ✗ 然後以為「更新功能壞了」✗ 這種 bug 極難查。
    ★ 為什麼 dry_run 存在：更新腳本／`--check` 流程要先給人看指令再動手 ✗
      而且它讓「指令長什麼樣」可以被測試釘住。
    """
    command = [sys.executable, "-m", "pip", "install", "--upgrade", str(Path(wheel))]
    if dry_run:
        return {"installed": False, "stdout": "", "stderr": "", "command": command}

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise UpdateError("pip install 逾時（%d 秒）：%s" % (APPLY_TIMEOUT, " ".join(command))) from e
    except OSError as e:
        raise UpdateError("叫不起 pip（%s）：%s" % (command[0], e)) from e

    return {
        "installed": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "command": command,
    }
