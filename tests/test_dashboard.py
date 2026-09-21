"""dashboard.py 的離線測試。

- 不連外網、不佔用固定埠（伺服器一律 port=0，靠 ready_file 拿實際位址）
- 所有 HTTP 請求都設 timeout，伺服器跑在 daemon thread，測試結束必定關閉
- config 的路徑是模組載入時固定的，故一律 monkeypatch ``teleshield.config.*``
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

import teleshield.config as config
import teleshield.dashboard as dashboard

#: 固定基準時間，讓所有跨日／跨時區斷言可重現。
NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """把所有資料檔指向 tmp_path。"""
    monkeypatch.setattr(config, "HOME_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "user.session")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "BLOCK_LOG", tmp_path / "block_log.json")
    monkeypatch.setattr(config, "LEARNED_FILE", tmp_path / "learned_patterns.json")
    return tmp_path


# ──────────────────────────── 測試工具 ────────────────────────────


def make_entry(user_id=1, name="某人", reason="廣告", source="private", when=None):
    """組一筆 block_log 條目（when 可傳 datetime 或原字串）。"""
    when = NOW if when is None else when
    return {
        "user_id": user_id,
        "name": name,
        "reason": reason,
        "source": source,
        "time": when.isoformat() if isinstance(when, datetime) else str(when),
    }


def write_blocks(entries):
    config.save_block_log({"blocks": list(entries)})


def raw_request(port, raw: bytes, timeout: float = 5.0) -> bytes:
    """用裸 socket 送原始請求（用來測 Content-Length 的邊界，避免大 body 卡住）。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(raw)
        chunks = []
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)


@pytest.fixture
def live_server(tmp_path):
    """以 port=0 啟動真正的伺服器，等 ready_file 出現後才讓測試打 API。"""
    ready = tmp_path / "ready.json"
    thread = threading.Thread(
        target=dashboard.serve,
        kwargs={"host": "127.0.0.1", "port": 0, "ready_file": ready},
        daemon=True,
    )
    thread.start()

    info = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if ready.exists():
            try:
                info = json.loads(ready.read_text(encoding="utf-8"))
                break
            except (ValueError, OSError):
                info = None
        time.sleep(0.02)

    assert info is not None, "ready_file 未在時限內出現（或內容不完整）"
    assert info["port"] > 0, "port=0 時應回報實際挑選的埠"
    assert info["url"].endswith(f":{info['port']}/")

    yield {"base": info["url"].rstrip("/"), "host": info["host"], "port": info["port"], "info": info}

    dashboard.shutdown_active_servers()
    thread.join(timeout=5)
    assert not thread.is_alive(), "伺服器未隨 shutdown 結束"
    assert not ready.exists(), "離開時應清掉 ready_file"


def http_get(base, path, timeout=5.0):
    req = urllib.request.Request(base + path, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def http_post(base, path, payload=None, raw_body=None, timeout=5.0):
    body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def read_json(resp):
    return json.loads(resp.read().decode("utf-8"))


def expect_400(base, path, payload=None, raw_body=None):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        http_post(base, path, payload=payload, raw_body=raw_body)
    assert excinfo.value.code == 400
    return json.loads(excinfo.value.read().decode("utf-8"))


# ──────────────────────────── build_stats ────────────────────────────


class TestBuildStats:
    def test_empty_log(self):
        stats = dashboard.build_stats(now=NOW)
        assert stats["totals"] == {
            "blocks": 0,
            "today": 0,
            "week": 0,
            "month": 0,
            "all_time": 0,
            "kicked": 0,
        }
        assert stats["by_source"] == {"private": 0, "group": 0}
        assert stats["by_hour"] == [0] * 24
        assert stats["top_reasons"] == []
        assert stats["recent"] == []
        assert stats["lists"] == {"whitelist": 0, "blacklist": 0, "learned_keywords": 0}
        assert stats["account"] == {"username": "", "user_id": "", "last_scan": ""}

    def test_empty_log_by_day_is_consecutive_days(self):
        by_day = dashboard.build_stats(now=NOW)["by_day"]
        assert [day["date"] for day in by_day] == [
            "2026-09-15",
            "2026-09-16",
            "2026-09-17",
            "2026-09-18",
            "2026-09-19",
            "2026-09-20",
            "2026-09-21",
        ]
        assert all(day["count"] == 0 for day in by_day)

    def test_by_day_custom_length(self):
        by_day = dashboard.build_stats(days=3, now=NOW)["by_day"]
        assert [day["date"] for day in by_day] == ["2026-09-19", "2026-09-20", "2026-09-21"]
        assert len(dashboard.build_stats(days=1, now=NOW)["by_day"]) == 1
        assert len(dashboard.build_stats(days=0, now=NOW)["by_day"]) == 1  # 下限保護

    def test_single_entry(self):
        write_blocks([make_entry(user_id=7, reason="加我微信")])
        stats = dashboard.build_stats(now=NOW)
        assert stats["totals"]["blocks"] == 1
        assert stats["totals"]["today"] == 1
        assert stats["totals"]["week"] == 1
        assert stats["totals"]["month"] == 1
        assert stats["totals"]["all_time"] == 1
        assert stats["by_day"][-1] == {"date": "2026-09-21", "count": 1}
        assert stats["by_hour"][NOW.hour] == 1
        assert stats["top_reasons"] == [{"reason": "加我微信", "count": 1}]
        assert len(stats["recent"]) == 1
        assert stats["recent"][0]["user_id"] == 7

    def test_day_boundary(self):
        # 今天 00:00:00Z 屬於今天；前一天 23:59:59Z 只算近 7 天
        write_blocks(
            [
                make_entry(user_id=1, when=datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)),
                make_entry(user_id=2, when=datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc)),
            ]
        )
        stats = dashboard.build_stats(now=NOW)
        assert stats["totals"]["today"] == 1
        assert stats["totals"]["week"] == 2
        assert stats["by_day"][-1] == {"date": "2026-09-21", "count": 1}
        assert stats["by_day"][-2] == {"date": "2026-09-20", "count": 1}

    def test_week_and_month_are_rolling_windows(self):
        write_blocks(
            [
                make_entry(when=NOW - timedelta(days=1)),
                make_entry(when=NOW - timedelta(days=10)),
                make_entry(when=NOW - timedelta(days=40)),
            ]
        )
        totals = dashboard.build_stats(now=NOW)["totals"]
        assert totals["blocks"] == 3
        assert totals["week"] == 1
        assert totals["month"] == 2
        assert totals["today"] == 0

    def test_timezone_offsets_bucketed_in_utc(self):
        # 基準 2026-09-21T02:00Z：+08:00 的 09:00 == 01:00Z（今天），07:00 == 前一日 23:00Z
        base = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
        tz8 = timezone(timedelta(hours=8))
        write_blocks(
            [
                make_entry(user_id=1, when=datetime(2026, 9, 21, 9, 0, tzinfo=tz8)),
                make_entry(user_id=2, when=datetime(2026, 9, 21, 7, 0, tzinfo=tz8)),
            ]
        )
        stats = dashboard.build_stats(now=base)
        assert stats["totals"]["today"] == 1
        assert stats["by_day"][-1] == {"date": "2026-09-21", "count": 1}
        assert stats["by_day"][-2] == {"date": "2026-09-20", "count": 1}

    def test_z_suffix_and_naive_time_are_utc(self):
        write_blocks(
            [
                make_entry(user_id=1, when="2026-09-21T05:00:00Z"),
                make_entry(user_id=2, when="2026-09-21T06:00:00"),  # naive -> 視為 UTC
            ]
        )
        stats = dashboard.build_stats(now=NOW)
        assert stats["totals"]["blocks"] == 2
        assert stats["totals"]["today"] == 2
        assert stats["by_hour"][5] == 1 and stats["by_hour"][6] == 1

    def test_unparseable_entries_are_ignored(self):
        write_blocks([{"user_id": 1, "reason": "x", "source": "private", "time": "not-a-time"}])
        stats = dashboard.build_stats(now=NOW)
        assert stats["totals"]["blocks"] == 0
        assert stats["by_hour"] == [0] * 24
        assert len(stats["recent"]) == 1  # recent 仍保留原條目，方便排查

    def test_by_hour_shape_and_counts(self):
        write_blocks(
            [
                make_entry(when=datetime(2026, 9, 21, 0, 30, tzinfo=timezone.utc)),
                make_entry(when=datetime(2026, 9, 21, 12, 1, tzinfo=timezone.utc)),
                make_entry(when=datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc)),
                make_entry(when=datetime(2026, 9, 21, 23, 59, 30, tzinfo=timezone.utc)),
            ]
        )
        by_hour = dashboard.build_stats(now=NOW)["by_hour"]
        assert len(by_hour) == 24
        assert by_hour[0] == 1 and by_hour[12] == 1 and by_hour[23] == 2
        assert sum(by_hour) == 4

    def test_by_source_private_and_group(self):
        write_blocks(
            [
                make_entry(source="private"),
                make_entry(source="group"),
                make_entry(source="group"),
            ]
        )
        assert dashboard.build_stats(now=NOW)["by_source"] == {"private": 1, "group": 2}

    def test_top_reasons_sorted_and_capped_at_10(self):
        entries = []
        for index in range(12):
            for _ in range(index + 1):
                entries.append(make_entry(reason=f"原因{index:02d}"))
        write_blocks(entries)
        top = dashboard.build_stats(now=NOW)["top_reasons"]
        assert len(top) == 10
        counts = [item["count"] for item in top]
        assert counts == sorted(counts, reverse=True)
        assert top[0] == {"reason": "原因11", "count": 12}
        assert "原因00" not in [item["reason"] for item in top]

    def test_top_reasons_tie_break_is_deterministic(self):
        write_blocks([make_entry(reason="b"), make_entry(reason="a")])
        top = dashboard.build_stats(now=NOW)["top_reasons"]
        assert [item["reason"] for item in top] == ["a", "b"]

    def test_empty_reason_bucket(self):
        write_blocks([make_entry(reason="")])
        assert dashboard.build_stats(now=NOW)["top_reasons"] == [{"reason": "（未註明）", "count": 1}]

    def test_all_time_takes_larger_of_config_and_log(self):
        config.save_config({"blocked_count": 999, "kicked_count": 42})
        write_blocks([make_entry(), make_entry()])
        totals = dashboard.build_stats(now=NOW)["totals"]
        assert totals["all_time"] == 999
        assert totals["kicked"] == 42
        assert totals["blocks"] == 2

    def test_all_time_falls_back_to_log_when_config_missing(self):
        write_blocks([make_entry(), make_entry(), make_entry()])
        assert dashboard.build_stats(now=NOW)["totals"]["all_time"] == 3

    def test_recent_is_newest_first_and_capped(self):
        write_blocks([make_entry(user_id=i, when=NOW + timedelta(minutes=i)) for i in range(1, 26)])
        recent = dashboard.build_stats(now=NOW)["recent"]
        assert len(recent) == 20
        assert recent[0]["user_id"] == 25
        assert recent[-1]["user_id"] == 6

    def test_lists_and_account(self):
        config.save_config(
            {
                "whitelist": {"1": {"username": "甲"}},
                "blacklist": {"2": {}, "3": {}},
                "learned_patterns": {"keywords": ["加我", "稳赚", "投资"]},
                "username": "老大",
                "user_id": 123456,
                "last_scan": "2026-09-21 10:00",
            }
        )
        stats = dashboard.build_stats(now=NOW)
        assert stats["lists"] == {"whitelist": 1, "blacklist": 2, "learned_keywords": 3}
        assert stats["account"] == {
            "username": "老大",
            "user_id": "123456",
            "last_scan": "2026-09-21 10:00",
        }

    def test_learned_keywords_from_standalone_file(self):
        config.save_learned_patterns({"keywords": ["a", "b", "c", "d"], "patterns": []})
        assert dashboard.build_stats(now=NOW)["lists"]["learned_keywords"] == 4

    def test_corrupt_files_do_not_raise(self, isolated_home):
        (isolated_home / "block_log.json").write_text("{broken", encoding="utf-8")
        (isolated_home / "config.json").write_text("[]", encoding="utf-8")
        stats = dashboard.build_stats(now=NOW)
        assert stats["totals"]["blocks"] == 0
        assert stats["lists"]["whitelist"] == 0


# ──────────────────────────── 名單驗證與寫入 ────────────────────────────


class TestValidateChange:
    def test_accepts_and_normalises(self):
        assert dashboard.validate_change(
            {"list": "whitelist", "action": "add", "user_id": " 123 ", "name": " 小明 "}
        ) == {"list_type": "whitelist", "action": "add", "user_id": "123", "name": "小明"}

    def test_accepts_int_user_id(self):
        assert dashboard.validate_change(
            {"list": "blacklist", "action": "remove", "user_id": 456}
        )["user_id"] == "456"

    def test_name_is_optional(self):
        assert dashboard.validate_change(
            {"list": "whitelist", "action": "add", "user_id": "1"}
        )["name"] == ""

    @pytest.mark.parametrize(
        "payload",
        [
            {"list": "friends", "action": "add", "user_id": "1"},
            {"list": "whitelist", "action": "drop", "user_id": "1"},
            {"list": "whitelist", "action": "add", "user_id": "abc"},
            {"list": "whitelist", "action": "add", "user_id": "-5"},
            {"list": "whitelist", "action": "add", "user_id": ""},
            {"list": "whitelist", "action": "add", "user_id": True},
            {"list": "whitelist", "action": "add", "user_id": "9" * 40},
            {"list": "whitelist", "action": "add", "user_id": "1", "name": 5},
            {"list": None, "action": "add", "user_id": "1"},
            ["not", "a", "dict"],
        ],
    )
    def test_rejects_bad_payloads(self, payload):
        with pytest.raises(ValueError):
            dashboard.validate_change(payload)

    def test_long_name_truncated(self):
        result = dashboard.validate_change(
            {"list": "whitelist", "action": "add", "user_id": "1", "name": "x" * 300}
        )
        assert len(result["name"]) == 120


class TestApplyChange:
    def test_add_writes_config(self):
        config.save_config({"username": "老大", "blocked_count": 5})
        dashboard.apply_change("whitelist", "add", "777", "阿明")
        cfg = config.load_config()
        assert cfg["whitelist"]["777"]["username"] == "阿明"
        assert cfg["whitelist"]["777"]["reason"] == "manual"
        assert cfg["whitelist"]["777"]["added"]

    def test_add_preserves_other_fields(self):
        config.save_config(
            {
                "username": "老大",
                "user_id": 1,
                "blocked_count": 5,
                "kicked_count": 2,
                "last_scan": "2026-09-21",
                "blacklist": {"9": {"username": "壞人"}},
            }
        )
        dashboard.apply_change("blacklist", "add", "555", "廣告機")
        cfg = config.load_config()
        assert cfg["username"] == "老大"
        assert cfg["blocked_count"] == 5 and cfg["kicked_count"] == 2
        assert cfg["last_scan"] == "2026-09-21"
        assert cfg["blacklist"]["9"]["username"] == "壞人"  # 舊資料不動
        assert set(cfg["blacklist"]) == {"9", "555"}

    def test_remove_deletes_only_target(self):
        config.save_config({"whitelist": {"1": {"username": "甲"}, "2": {"username": "乙"}}})
        dashboard.apply_change("whitelist", "remove", "1")
        cfg = config.load_config()
        assert set(cfg["whitelist"]) == {"2"}

    def test_remove_missing_id_is_noop(self):
        config.save_config({"whitelist": {"1": {}}})
        dashboard.apply_change("whitelist", "remove", "404")
        assert set(config.load_config()["whitelist"]) == {"1"}

    def test_add_existing_keeps_added_date(self):
        config.save_config({"whitelist": {"1": {"added": "2020-01-01", "username": "舊"}}})
        dashboard.apply_change("whitelist", "add", "1", "新")
        entry = config.load_config()["whitelist"]["1"]
        assert entry["added"] == "2020-01-01"
        assert entry["username"] == "新"


# ──────────────────────────── render_html ────────────────────────────


class TestRenderHtml:
    def test_no_external_resources(self):
        page = dashboard.render_html(dashboard.build_stats(now=NOW))
        assert page.startswith("<!DOCTYPE html>")
        assert "http://" not in page and "https://" not in page
        assert "//cdn" not in page and "fonts.googleapis" not in page
        assert "@import" not in page and "<link" not in page
        assert "tabular-nums" in page

    def test_escapes_untrusted_text(self):
        write_blocks([make_entry(name="<script>alert(1)</script>", reason='"><img src=x>')])
        page = dashboard.render_html(dashboard.build_stats(now=NOW))
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
        assert '"><img src=x>' not in page

    def test_handles_empty_stats(self):
        page = dashboard.render_html({})
        assert "TeleShield" in page


# ──────────────────────────── HTTP ────────────────────────────


class TestHttp:
    def test_index_returns_html(self, live_server):
        resp = http_get(live_server["base"], "/")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert "no-store" in resp.headers["Cache-Control"]
        body = resp.read().decode("utf-8")
        assert "<!DOCTYPE html>" in body
        assert "/api/lists" in body

    def test_api_stats(self, live_server):
        write_blocks([make_entry(reason="廣告")])
        config.save_config({"username": "老大", "blocked_count": 3})
        resp = http_get(live_server["base"], "/api/stats")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        assert "no-store" in resp.headers["Cache-Control"]
        data = read_json(resp)
        assert data["totals"]["blocks"] == 1
        assert data["totals"]["all_time"] == 3
        assert data["account"]["username"] == "老大"
        assert len(data["by_day"]) == 7

    def test_api_lists_exposes_only_id_and_name(self, live_server):
        config.save_config(
            {
                "whitelist": {
                    "123": {"username": "小明", "added": "2026-01-01", "reason": "manual"}
                },
                "blacklist": {"9": {}},
            }
        )
        resp = http_get(live_server["base"], "/api/lists")
        assert resp.status == 200
        raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        assert data == {"whitelist": {"123": "小明"}, "blacklist": {"9": ""}}
        assert "manual" not in raw and "added" not in raw and "2026-01-01" not in raw

    def test_post_add_and_remove_via_http(self, live_server):
        config.save_config({"username": "老大", "blocked_count": 7})

        resp = http_post(
            live_server["base"],
            "/api/lists",
            {"list": "whitelist", "action": "add", "user_id": "555", "name": "小明"},
        )
        assert resp.status == 200
        assert read_json(resp) == {"ok": True}
        cfg = config.load_config()
        assert cfg["whitelist"]["555"]["username"] == "小明"
        assert cfg["username"] == "老大" and cfg["blocked_count"] == 7

        resp = http_post(
            live_server["base"],
            "/api/lists",
            {"list": "whitelist", "action": "remove", "user_id": "555"},
        )
        assert resp.status == 200
        assert read_json(resp) == {"ok": True}
        assert "555" not in config.load_config()["whitelist"]

    def test_post_blacklist_add(self, live_server):
        resp = http_post(
            live_server["base"],
            "/api/lists",
            {"list": "blacklist", "action": "add", "user_id": 42, "name": "廣告機"},
        )
        assert resp.status == 200
        assert config.load_config()["blacklist"]["42"]["username"] == "廣告機"

    @pytest.mark.parametrize(
        "payload",
        [
            {"list": "friends", "action": "add", "user_id": "1"},
            {"list": "whitelist", "action": "delete", "user_id": "1"},
            {"list": "whitelist", "action": "add", "user_id": "not-a-number"},
            {"list": "whitelist", "action": "add", "user_id": ""},
            {"list": "whitelist", "action": "add", "user_id": 3.5},
        ],
    )
    def test_post_rejects_bad_shapes(self, live_server, payload):
        error = expect_400(live_server["base"], "/api/lists", payload)
        assert error["ok"] is False
        assert error["error"]

    def test_post_rejects_non_json(self, live_server):
        error = expect_400(live_server["base"], "/api/lists", raw_body=b"this is not json")
        assert error["ok"] is False

    def test_post_rejects_json_non_object(self, live_server):
        error = expect_400(live_server["base"], "/api/lists", raw_body=b'["whitelist"]')
        assert error["ok"] is False

    def test_post_rejects_empty_body(self, live_server):
        error = expect_400(live_server["base"], "/api/lists", raw_body=b"")
        assert error["ok"] is False

    def test_post_rejects_oversized_content_length(self, live_server):
        # 不真的送 70KB：聲明超大的 Content-Length，伺服器必須在讀 body 前就拒絕
        raw = (
            "POST /api/lists HTTP/1.0\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {dashboard.MAX_BODY_BYTES + 1}\r\n"
            "\r\n"
        ).encode("ascii")
        response = raw_request(live_server["port"], raw)
        assert b" 400 " in response.split(b"\r\n", 1)[0]
        assert b"error" in response
        assert config.load_config() == {}  # 沒有寫入任何東西

    def test_post_rejects_missing_content_length(self, live_server):
        raw = b"POST /api/lists HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
        response = raw_request(live_server["port"], raw)
        assert b" 400 " in response.split(b"\r\n", 1)[0]

    def test_post_unknown_path_404(self, live_server):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            http_post(live_server["base"], "/api/nope", {"list": "whitelist"})
        assert excinfo.value.code == 404

    @pytest.mark.parametrize("path", ["/nope", "/api/nope", "/../etc/passwd", "/index.html.bak"])
    def test_unknown_paths_404(self, live_server, path):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            http_get(live_server["base"], path)
        assert excinfo.value.code == 404

    def test_root_does_not_serve_arbitrary_files(self, live_server, isolated_home):
        (isolated_home / "secret.txt").write_text("top secret", encoding="utf-8")
        with pytest.raises(urllib.error.HTTPError):
            http_get(live_server["base"], "/secret.txt")

    def test_lists_reflect_daemon_writes(self, live_server):
        # 模擬 daemon 在伺服器啟動後才寫入 config
        config.save_config({"whitelist": {"321": {"username": "後寫"}}})
        data = read_json(http_get(live_server["base"], "/api/lists"))
        assert data["whitelist"] == {"321": "後寫"}
