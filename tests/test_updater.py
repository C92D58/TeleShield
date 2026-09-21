"""updater.py 測試 — **全部離線**（注入假 fetcher 或攔截 urlopen）。

★ 為什麼要有一個 autouse 的 no_network：
  漏掉任何一個 fetcher 就會真的打到 api.github.com ✗ 在 CI 上代表慢、flaky、
  還可能因為未認證的 rate limit（每小時 60 次）而隨機變紅 ✗
  這條 fixture 讓「不小心連網」立刻變成 AssertionError。
"""

import hashlib
import json
import subprocess
import sys
import urllib.error

import pytest

import teleshield.updater as updater
from teleshield.updater import (
    Release,
    UpdateError,
    apply,
    check,
    checksum,
    download,
    fetch_release,
    find_asset,
    parse_version,
    verify_checksum,
)

WHEEL = "teleshield-0.11.0-py3-none-any.whl"
SDIST = "teleshield-0.11.0.tar.gz"
WHEEL_URL = "https://example.invalid/teleshield-0.11.0-py3-none-any.whl"
SDIST_URL = "https://example.invalid/teleshield-0.11.0.tar.gz"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """任何沒被注入 fetcher 的呼叫都會撞到這顆炸彈 ✗ 測試不許連網。"""

    def boom(*args, **kwargs):
        raise AssertionError("測試不許連網：urllib.request.urlopen 被呼叫了")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("TELESHIELD_GITHUB_TOKEN", raising=False)


def make_payload(tag="v0.11.0", *, name=None, body="- 修好了換行", assets=None,
                 published_at="2026-09-21T10:00:00Z"):
    """造一個 GitHub `/releases/latest` 形狀的回應。"""
    return {
        "tag_name": tag,
        "name": name if name is not None else "TeleShield " + tag.lstrip("v"),
        "body": body,
        "html_url": "https://github.com/C92D58/TeleShield/releases/tag/" + tag,
        "published_at": published_at,
        "assets": assets if assets is not None else [
            {"name": WHEEL, "browser_download_url": WHEEL_URL, "size": 12345,
             "digest": "sha256:" + "a" * 64},
            {"name": SDIST, "browser_download_url": SDIST_URL, "size": 23456,
             "digest": "sha256:" + "b" * 64},
        ],
    }


def fetcher_returning(payload, calls=None):
    """假 fetcher：回傳 payload 的 JSON bytes，順便記錄呼叫參數。"""

    def _fetch(url, timeout, token):
        if calls is not None:
            calls.append((url, timeout, token))
        return json.dumps(payload).encode("utf-8")

    return _fetch


def fetcher_raising(exc):
    """假 fetcher：直接拋錯（模擬 HTTP 404 / 連線失敗）。"""

    def _fetch(url, timeout, token):
        raise exc

    return _fetch


def make_release(tag="v0.11.0", assets=None) -> Release:
    return fetch_release(fetcher=fetcher_returning(make_payload(tag, assets=assets)))


# ════════════════════════════════════════════════════════════════════
# parse_version
# ════════════════════════════════════════════════════════════════════
class TestParseVersion:
    def test_minor_ten_beats_minor_nine(self):
        # 字串比較的經典陷阱："0.10.0" < "0.9.0" ✗ tuple 比較必須是 0.9.0 < 0.10.0
        assert parse_version("0.9.0") < parse_version("0.10.0")

    def test_v_prefix_is_the_same_version(self):
        assert parse_version("v1.2.3") == parse_version("1.2.3")

    def test_prerelease_sorts_below_final(self):
        assert parse_version("1.2.3-rc1") < parse_version("1.2.3")

    def test_newer_minor_beats_older_patch(self):
        assert parse_version("0.11.0") > parse_version("0.9.9")

    def test_missing_components_are_zero(self):
        # "1.2" 與 "1.2.0" 是同一版 ✗ 不補零的話前者會被判成較小 ✗ 誤報更新
        assert parse_version("1.2") == parse_version("1.2.0")

    def test_longer_core_is_newer(self):
        assert parse_version("1.2.0.1") > parse_version("1.2.0")
        assert parse_version("2.1") > parse_version("2")
        # ★ 補零的必然結果：少寫的位數視為 0 ✗ "2" 就是 "2.0"
        assert parse_version("2") == parse_version("2.0") == parse_version("2.0.0")

    def test_prerelease_number_orders_within_rc(self):
        assert parse_version("0.11.0-rc1") < parse_version("0.11.0-rc2")
        assert parse_version("0.11.0-rc2") < parse_version("0.11.0")

    def test_prerelease_kind_ranks(self):
        assert parse_version("1.0.0-alpha1") < parse_version("1.0.0-beta1")
        assert parse_version("1.0.0-beta1") < parse_version("1.0.0-rc1")
        assert parse_version("1.0.0-rc1") < parse_version("1.0.0")

    def test_garbage_never_raises(self):
        # tag 是自由文字 ✗ 認不出來的東西一律當 0.0.0 ✗ 更新檢查不該讓 CLI 當掉
        for junk in ("", None, "latest", "v", "..."):
            assert parse_version(junk) == parse_version("0.0.0")

    def test_returns_tuple(self):
        assert isinstance(parse_version("v0.11.0"), tuple)


# ════════════════════════════════════════════════════════════════════
# fetch_release
# ════════════════════════════════════════════════════════════════════
class TestFetchRelease:
    def test_parses_payload(self):
        calls = []
        rel = fetch_release("C92D58/TeleShield", timeout=7.5,
                            fetcher=fetcher_returning(make_payload(), calls))
        assert rel.tag == "v0.11.0"
        assert rel.version == "0.11.0"          # ★ v 前綴要去掉
        assert rel.name == "TeleShield 0.11.0"
        assert rel.body == "- 修好了換行"
        assert rel.html_url.endswith("/v0.11.0")
        assert rel.published_at == "2026-09-21T10:00:00Z"
        assert [a["name"] for a in rel.assets] == [WHEEL, SDIST]
        assert calls[0][0] == "https://api.github.com/repos/C92D58/TeleShield/releases/latest"
        assert calls[0][1] == 7.5               # timeout 要傳到 fetcher

    def test_missing_optional_fields_get_defaults(self):
        rel = fetch_release(fetcher=fetcher_returning({"tag_name": "v1.0.0"}))
        assert rel.version == "1.0.0"
        assert rel.name == "v1.0.0"
        assert rel.body == ""
        assert rel.assets == []
        assert rel.html_url.startswith("https://github.com/")

    def test_404_is_readable(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with pytest.raises(UpdateError) as excinfo:
            fetch_release("C92D58/TeleShield", fetcher=fetcher_raising(err))
        message = str(excinfo.value)
        assert "404" in message
        assert "C92D58/TeleShield" in message

    def test_403_rate_limit_is_named(self):
        err = urllib.error.HTTPError(
            "u", 403, "Forbidden", {"X-RateLimit-Remaining": "0"}, None)
        with pytest.raises(UpdateError) as excinfo:
            fetch_release(fetcher=fetcher_raising(err))
        message = str(excinfo.value)
        assert "403" in message
        assert "rate limit" in message.lower()
        assert "GITHUB_TOKEN" in message

    def test_401_token_hint(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with pytest.raises(UpdateError) as excinfo:
            fetch_release(fetcher=fetcher_raising(err))
        assert "401" in str(excinfo.value)

    def test_bad_json_is_readable(self):
        with pytest.raises(UpdateError) as excinfo:
            fetch_release(fetcher=lambda url, timeout, token: b"<html>502 Bad Gateway</html>")
        message = str(excinfo.value)
        assert "JSON" in message
        assert "502" in message                 # 原文片段要留下來 ✗ 不然沒線索

    def test_network_error_becomes_update_error(self):
        err = urllib.error.URLError("no route to host")
        with pytest.raises(UpdateError) as excinfo:
            fetch_release(fetcher=fetcher_raising(err))
        assert "no route to host" in str(excinfo.value)

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(UpdateError):
            fetch_release(fetcher=lambda url, timeout, token: b"[1, 2, 3]")

    def test_missing_tag_name_is_rejected(self):
        with pytest.raises(UpdateError) as excinfo:
            fetch_release(fetcher=fetcher_returning({"name": "沒有 tag"}))
        assert "tag_name" in str(excinfo.value)

    def test_repo_must_be_owner_slash_name(self):
        with pytest.raises(UpdateError):
            fetch_release("TeleShield", fetcher=fetcher_returning(make_payload()))

    def test_token_from_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        calls = []
        fetch_release(fetcher=fetcher_returning(make_payload(), calls))
        assert calls[0][2] == "env-token"

    def test_project_specific_token_env(self, monkeypatch):
        monkeypatch.setenv("TELESHIELD_GITHUB_TOKEN", "shield-token")
        calls = []
        fetch_release(fetcher=fetcher_returning(make_payload(), calls))
        assert calls[0][2] == "shield-token"

    def test_explicit_token_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        calls = []
        fetch_release(token="explicit-token", fetcher=fetcher_returning(make_payload(), calls))
        assert calls[0][2] == "explicit-token"


# ════════════════════════════════════════════════════════════════════
# check
# ════════════════════════════════════════════════════════════════════
class TestCheck:
    def test_update_available(self):
        result = check("0.10.0", fetcher=fetcher_returning(make_payload("v0.11.0")))
        assert result["update_available"] is True
        assert result["current"] == "0.10.0"
        assert result["latest"] == "0.11.0"
        assert result["notes"] == "- 修好了換行"
        assert isinstance(result["release"], Release)

    def test_same_version_no_update(self):
        result = check("0.11.0", fetcher=fetcher_returning(make_payload("v0.11.0")))
        assert result["update_available"] is False

    def test_older_remote_never_offers_a_downgrade(self):
        # ★ 本機比 release 新（開發版）時不能叫使用者降級
        result = check("0.12.0", fetcher=fetcher_returning(make_payload("v0.11.0")))
        assert result["update_available"] is False
        assert result["latest"] == "0.11.0"

    def test_prerelease_remote_is_an_update_for_older_final(self):
        result = check("0.10.0", fetcher=fetcher_returning(make_payload("v0.11.0-rc1")))
        assert result["update_available"] is True

    def test_prerelease_remote_is_not_an_update_for_same_final(self):
        result = check("0.11.0", fetcher=fetcher_returning(make_payload("v0.11.0-rc1")))
        assert result["update_available"] is False

    def test_current_defaults_to_installed_metadata(self, monkeypatch):
        monkeypatch.setattr(updater.importlib_metadata, "version", lambda name: "0.10.0")
        result = check(fetcher=fetcher_returning(make_payload("v0.11.0")))
        assert result["current"] == "0.10.0"
        assert result["update_available"] is True

    def test_current_falls_back_to_zero_when_not_installed(self, monkeypatch):
        def missing(name):
            raise updater.importlib_metadata.PackageNotFoundError(name)

        monkeypatch.setattr(updater.importlib_metadata, "version", missing)
        assert updater.current_version() == "0.0.0"
        result = check(fetcher=fetcher_returning(make_payload("v0.11.0")))
        assert result["current"] == "0.0.0"
        assert result["update_available"] is True

    def test_check_propagates_update_error(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with pytest.raises(UpdateError):
            check("0.10.0", fetcher=fetcher_raising(err))


# ════════════════════════════════════════════════════════════════════
# checksum / verify_checksum
# ════════════════════════════════════════════════════════════════════
class TestChecksum:
    def test_checksum_matches_hashlib(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"teleshield\n" * 10)
        assert checksum(blob) == hashlib.sha256(b"teleshield\n" * 10).hexdigest()

    def test_verify_upper_case_is_ok(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"payload")
        expected = hashlib.sha256(b"payload").hexdigest().upper()
        assert verify_checksum(blob, expected) is True

    def test_verify_accepts_sha256_prefix(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"payload")
        expected = "sha256:" + hashlib.sha256(b"payload").hexdigest()
        assert verify_checksum(blob, expected) is True
        assert verify_checksum(blob, "SHA256:" + hashlib.sha256(b"payload").hexdigest().upper()) is True

    def test_verify_mismatch_is_false(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"payload")
        assert verify_checksum(blob, "0" * 64) is False

    def test_verify_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            verify_checksum(tmp_path / "nope.bin", "a" * 64)

    def test_verify_empty_expected_raises(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"payload")
        with pytest.raises(ValueError):
            verify_checksum(blob, "")


# ════════════════════════════════════════════════════════════════════
# download
# ════════════════════════════════════════════════════════════════════
class TestDownload:
    def test_download_writes_dest_and_removes_part(self, tmp_path):
        data = b"wheel bytes" * 10
        calls = []
        dest = tmp_path / WHEEL

        def fake(url, timeout, token):
            calls.append((url, timeout, token))
            return data

        result = download(WHEEL_URL, dest, expected_sha256=hashlib.sha256(data).hexdigest(),
                          fetcher=fake)
        assert result == dest
        assert dest.read_bytes() == data
        assert not (tmp_path / (WHEEL + ".part")).exists()
        assert calls == [(WHEEL_URL, 60.0, None)]

    def test_download_without_expected_hash_still_works(self, tmp_path):
        dest = tmp_path / WHEEL
        download(WHEEL_URL, dest, fetcher=lambda url, timeout, token: b"raw")
        assert dest.read_bytes() == b"raw"

    def test_checksum_mismatch_deletes_the_file(self, tmp_path):
        dest = tmp_path / WHEEL
        with pytest.raises(UpdateError) as excinfo:
            download(WHEEL_URL, dest, expected_sha256="f" * 64,
                     fetcher=lambda url, timeout, token: b"evil payload")
        message = str(excinfo.value)
        assert "校驗碼不符" in message
        assert "f" * 64 in message              # 期望值要寫進訊息才有線索
        assert not dest.exists(), "驗不過的檔案不可以留在磁碟上"
        assert not (tmp_path / (WHEEL + ".part")).exists()

    def test_midway_failure_leaves_no_part_file(self, tmp_path):
        """★ 模擬「下載到一半斷線」：fetcher 先把 .part 寫出來再拋錯。"""
        dest = tmp_path / WHEEL
        part = tmp_path / (WHEEL + ".part")

        def half_then_die(url, timeout, token):
            part.write_bytes(b"half a wheel")
            raise urllib.error.URLError("連線中斷")

        with pytest.raises(UpdateError):
            download(WHEEL_URL, dest, fetcher=half_then_die)
        assert not part.exists(), "失敗路徑一定要收拾掉半個檔"
        assert not dest.exists()

    def test_http_error_is_translated(self, tmp_path):
        err = urllib.error.HTTPError(
            "u", 403, "Forbidden", {"X-RateLimit-Remaining": "0"}, None)
        dest = tmp_path / WHEEL
        with pytest.raises(UpdateError) as excinfo:
            download(WHEEL_URL, dest, fetcher=fetcher_raising(err))
        assert "403" in str(excinfo.value)
        assert not (tmp_path / (WHEEL + ".part")).exists()

    def test_creates_missing_parent_directory(self, tmp_path):
        dest = tmp_path / "dist" / WHEEL
        download(WHEEL_URL, dest, fetcher=lambda url, timeout, token: b"ok")
        assert dest.is_file()

    def test_none_bytes_from_fetcher_is_rejected(self, tmp_path):
        dest = tmp_path / WHEEL
        with pytest.raises(UpdateError):
            download(WHEEL_URL, dest, fetcher=lambda url, timeout, token: None)
        assert not dest.exists()


# ════════════════════════════════════════════════════════════════════
# find_asset
# ════════════════════════════════════════════════════════════════════
class TestFindAsset:
    def test_prefers_wheel_over_sdist(self):
        asset = find_asset(make_release())
        assert asset is not None
        assert asset["name"] == WHEEL
        assert asset["browser_download_url"] == WHEEL_URL

    def test_falls_back_to_tar_gz(self):
        release = make_release(assets=[{"name": "code.tar.gz", "browser_download_url": "https://example.invalid/c.tar.gz"}])
        asset = find_asset(release)
        assert asset is not None
        assert asset["name"] == "code.tar.gz"

    def test_prefer_overrides_the_default_order(self):
        asset = find_asset(make_release(), prefer=".tar.gz")
        assert asset is not None
        assert asset["name"] == SDIST

    def test_returns_none_when_nothing_matches(self):
        assert find_asset(make_release(assets=[{"name": "checksums.txt"}])) is None
        assert find_asset(make_release(assets=[])) is None
        assert find_asset(make_release(assets=[{"no_name": 1}, "not a dict"])) is None


# ════════════════════════════════════════════════════════════════════
# apply
# ════════════════════════════════════════════════════════════════════
class TestApply:
    def test_dry_run_does_not_execute(self, tmp_path, monkeypatch):
        wheel = tmp_path / WHEEL

        def boom(*args, **kwargs):
            raise AssertionError("dry_run 不可以真的跑 subprocess")

        monkeypatch.setattr(updater.subprocess, "run", boom)
        result = apply(wheel, dry_run=True)

        assert result["installed"] is False
        assert result["stdout"] == "" and result["stderr"] == ""
        assert result["command"] == [sys.executable, "-m", "pip", "install", "--upgrade", str(wheel)]
        assert "--upgrade" in result["command"]
        assert str(wheel) in result["command"]

    def test_runs_pip_install_and_reports_success(self, tmp_path, monkeypatch):
        wheel = tmp_path / WHEEL
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, stdout="Successfully installed teleshield\n", stderr="")

        monkeypatch.setattr(updater.subprocess, "run", fake_run)
        result = apply(wheel)

        assert result["installed"] is True
        assert "Successfully installed" in result["stdout"]
        assert seen["command"][1:4] == ["-m", "pip", "install"]
        assert seen["kwargs"]["timeout"] == updater.APPLY_TIMEOUT

    def test_non_zero_returncode_is_not_installed(self, tmp_path, monkeypatch):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="ERROR: no such file")

        monkeypatch.setattr(updater.subprocess, "run", fake_run)
        result = apply(tmp_path / WHEEL)
        assert result["installed"] is False
        assert "no such file" in result["stderr"]

    def test_timeout_becomes_update_error(self, tmp_path, monkeypatch):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=updater.APPLY_TIMEOUT)

        monkeypatch.setattr(updater.subprocess, "run", fake_run)
        with pytest.raises(UpdateError) as excinfo:
            apply(tmp_path / WHEEL)
        assert "逾時" in str(excinfo.value)

    def test_missing_interpreter_becomes_update_error(self, tmp_path, monkeypatch):
        def fake_run(command, **kwargs):
            raise OSError("Exec format error")

        monkeypatch.setattr(updater.subprocess, "run", fake_run)
        with pytest.raises(UpdateError):
            apply(tmp_path / WHEEL)
