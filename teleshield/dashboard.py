"""本機網頁儀表板（只用標準庫：http.server + json + html）。

端點
----
- ``GET  /``            單頁深色儀表板（CSS/JS 全部內嵌，無任何外部資源）
- ``GET  /api/stats``   封鎖統計 JSON（等同 :func:`build_stats`）
- ``GET  /api/lists``   黑白名單（**只回 id 與 name**，不外洩 reason/added 等欄位）
- ``POST /api/lists``   新增／移除名單（``{"list","action","user_id","name"}``）

安全設計
--------
- 預設只綁 ``127.0.0.1``；非本機位址需由呼叫方（或 ``--allow-remote``）明確指定。
- POST 一律檢查 ``Content-Length``（上限 :data:`MAX_BODY_BYTES`）並驗證 JSON 形狀、
  名單白名單（whitelist/blacklist）、動作白名單（add/remove）、``user_id`` 必須為數字。
- 所有回應都帶 ``no-store``（名單與統計屬敏感且即時變動，禁止中間層快取）。
- 沒有目錄列表、不讀取任意檔案，未知路徑一律 404。

時間一律使用 UTC（與 ``config.log_block`` 寫入的 ISO8601 一致）。
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import config as config_module

__all__ = [
    "MAX_BODY_BYTES",
    "DashboardHandler",
    "build_stats",
    "apply_change",
    "validate_change",
    "create_server",
    "serve",
    "shutdown_active_servers",
    "render_html",
    "main",
]

#: POST 內容上限（64KB）——儀表板只收極小的 JSON，超過一律拒絕。
MAX_BODY_BYTES = 64 * 1024

_ALLOWED_LISTS = ("whitelist", "blacklist")
_ALLOWED_ACTIONS = ("add", "remove")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})
_NAME_LIMIT = 120
_ID_LIMIT = 32
_REASON_LIMIT = 200
_RECENT_LIMIT = 20
_TOP_REASON_LIMIT = 10

_log = logging.getLogger("teleshield.dashboard")

_SERVERS_LOCK = threading.Lock()
_ACTIVE_SERVERS: list = []


# ──────────────────────────── 工具函式 ────────────────────────────


def _as_text(value, limit: int = _REASON_LIMIT) -> str:
    """把任意值轉成去空白、限長的字串（保證可 JSON 序列化）。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()[:limit]


def _parse_time(value) -> Optional[datetime]:
    """解析 ISO8601 為 UTC ``datetime``；無法解析回 ``None``。

    Python 3.9 的 ``fromisoformat`` 不吃結尾的 ``Z``，故先正規化；
    不帶時區的 naive 時間一律視為 UTC（與 ``log_block`` 的寫法一致）。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_entry(item: dict) -> dict:
    """只保留白名單欄位，確保不外洩其他資料且可 JSON 序列化。"""
    uid = item.get("user_id")
    if isinstance(uid, bool):
        uid = int(uid)
    elif not isinstance(uid, (int, str)):
        uid = "" if uid is None else str(uid)
    return {
        "user_id": uid,
        "name": _as_text(item.get("name")),
        "reason": _as_text(item.get("reason")),
        "source": _as_text(item.get("source"), 32) or "private",
        "time": _as_text(item.get("time"), 64),
    }


def _int_or_zero(value) -> int:
    """寬鬆地把 config 的計數欄位轉成整數（壞資料不炸儀表板）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _entry_count(value) -> int:
    """dict/list 之類的容器取長度，其他型別回 0。"""
    if isinstance(value, (dict, list, tuple)):
        return len(value)
    return 0


def _learned_keyword_count(cfg: dict) -> int:
    """學習關鍵詞數量。

    同一份資料可能同時存在 ``config.json`` 的 ``learned_patterns`` 與獨立的
    ``learned_patterns.json``，兩者取較大值以免低估。
    """
    from_config = 0
    lp = cfg.get("learned_patterns")
    if isinstance(lp, dict):
        from_config = _entry_count(lp.get("keywords"))
    from_file = 0
    try:
        data = config_module.load_learned_patterns()
    except Exception:  # noqa: BLE001 — 讀檔失敗不該讓儀表板掛掉
        data = {}
    if isinstance(data, dict):
        from_file = _entry_count(data.get("keywords"))
    return max(from_config, from_file)


def _public_list(cfg: dict, key: str) -> dict:
    """把名單攤成 ``{id: name}``——只曝露 id 與名稱。"""
    raw = cfg.get(key)
    out = {}
    if not isinstance(raw, dict):
        return out
    for uid, info in raw.items():
        if isinstance(info, dict):
            name = info.get("username") or info.get("name") or ""
        elif isinstance(info, str):
            name = info
        else:
            name = ""
        out[str(uid)] = _as_text(name, _NAME_LIMIT)
    return out


# ──────────────────────────── 統計 ────────────────────────────


def build_stats(*, days: int = 7, now=None) -> dict:
    """彙整封鎖統計，回傳純資料 dict（不碰 HTTP，可單獨測試）。

    參數
    ----
    days: ``by_day`` 要連續涵蓋的天數（含今天），至少 1、上限 366。
    now:  計算基準時間；``None`` 取 ``datetime.now(timezone.utc)``。

    回傳
    ----
    ``totals``      : ``{blocks, today, week, month, all_time, kicked}``。
                      ``week``/``month`` 為滾動的過去 7／30 天（與 CLI 的
                      ``--report`` 一致）；``all_time`` 取「config 累計值」與
                      「日誌筆數」較大者（block_log 只保留 500 筆，config 的
                      累計值可能更大）。時間無法解析的條目一律忽略。
    ``by_source``   : ``{"private": n, "group": n}``；其他來源才動態加鍵。
    ``by_day``      : 連續 ``days`` 天（由舊到新），沒有記錄的補 0。
    ``by_hour``     : 長度 24 的 UTC 小時計數。
    ``top_reasons`` : 依次數遞減（同次數按原因排序）取前 10。
    ``recent``      : 最近 20 筆封鎖條目（新→舊）。
    ``lists``       : ``{whitelist, blacklist, learned_keywords}`` 數量。
    ``account``     : ``{username, user_id, last_scan}``。
    """
    if now is None:
        now_dt = datetime.now(timezone.utc)
    else:
        now_dt = _parse_time(now) or datetime.now(timezone.utc)

    if not isinstance(days, int) or isinstance(days, bool):
        days = 7
    days = max(1, min(int(days), 366))

    cfg = config_module.load_config()
    if not isinstance(cfg, dict):
        cfg = {}

    raw_blocks = config_module.load_block_log()
    if not isinstance(raw_blocks, dict):
        raw_blocks = {}
    raw_entries = raw_blocks.get("blocks")
    if not isinstance(raw_entries, list):
        raw_entries = []

    cleaned = [_clean_entry(item) for item in raw_entries if isinstance(item, dict)]

    today = now_dt.date()
    week_cut = now_dt - timedelta(days=7)
    month_cut = now_dt - timedelta(days=30)

    n_today = n_week = n_month = 0
    day_counts: dict = {}
    by_hour = [0] * 24
    by_source = {"private": 0, "group": 0}
    reason_counts: dict = {}

    for item in cleaned:
        dt = _parse_time(item["time"])
        if dt is None:
            continue  # 時間壞掉的條目無法歸日／歸時，整筆略過
        n_today += int(dt.date() == today)
        n_week += int(dt >= week_cut)
        n_month += int(dt >= month_cut)

        day_key = dt.strftime("%Y-%m-%d")
        day_counts[day_key] = day_counts.get(day_key, 0) + 1
        by_hour[dt.hour] += 1

        source = item["source"] or "private"
        by_source[source] = by_source.get(source, 0) + 1

        reason = item["reason"] or "（未註明）"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    total_parsed = sum(day_counts.values())

    by_day = []
    for offset in range(days - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        by_day.append({"date": key, "count": day_counts.get(key, 0)})

    top_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[
            :_TOP_REASON_LIMIT
        ]
    ]

    return {
        "totals": {
            "blocks": total_parsed,
            "today": n_today,
            "week": n_week,
            "month": n_month,
            "all_time": max(_int_or_zero(cfg.get("blocked_count", 0)), total_parsed),
            "kicked": _int_or_zero(cfg.get("kicked_count", 0)),
        },
        "by_source": by_source,
        "by_day": by_day,
        "by_hour": by_hour,
        "top_reasons": top_reasons,
        "recent": list(reversed(cleaned))[:_RECENT_LIMIT],
        "lists": {
            "whitelist": _entry_count(cfg.get("whitelist")),
            "blacklist": _entry_count(cfg.get("blacklist")),
            "learned_keywords": _learned_keyword_count(cfg),
        },
        "account": {
            "username": _as_text(cfg.get("username"), 64),
            "user_id": _as_text(cfg.get("user_id"), 64),
            "last_scan": _as_text(cfg.get("last_scan"), 64),
        },
    }


# ──────────────────────────── 名單寫入 ────────────────────────────


def validate_change(payload) -> dict:
    """驗證 POST /api/lists 的 JSON 形狀；不合法丟 ``ValueError``。

    回傳正規化後的 ``{"list_type", "action", "user_id", "name"}``。
    """
    if not isinstance(payload, dict):
        raise ValueError("請求內容必須是 JSON 物件")

    list_type = payload.get("list")
    if not isinstance(list_type, str) or list_type not in _ALLOWED_LISTS:
        raise ValueError("list 必須是 whitelist 或 blacklist")

    action = payload.get("action")
    if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
        raise ValueError("action 必須是 add 或 remove")

    raw_id = payload.get("user_id")
    if isinstance(raw_id, bool):
        raise ValueError("user_id 必須是數字")
    if isinstance(raw_id, int):
        user_id = str(raw_id)
    elif isinstance(raw_id, str):
        user_id = raw_id.strip()
    else:
        raise ValueError("user_id 必須是數字")
    if not user_id.isdigit() or len(user_id) > _ID_LIMIT:
        raise ValueError("user_id 必須是數字")

    raw_name = payload.get("name")
    if raw_name is None:
        name = ""
    elif isinstance(raw_name, str):
        name = raw_name.strip()[:_NAME_LIMIT]
    else:
        raise ValueError("name 必須是字串")

    return {"list_type": list_type, "action": action, "user_id": user_id, "name": name}


def apply_change(list_type: str, action: str, user_id: str, name: str = "") -> None:
    """把名單變更寫回 ``config.json``。

    **寫入前重新 ``load_config()``**：daemon 可能剛寫過 config，
    用舊快照寫回會蓋掉它的更新。
    """
    cfg = config_module.load_config()
    if not isinstance(cfg, dict):
        cfg = {}

    current = cfg.get(list_type)
    if not isinstance(current, dict):
        current = {}
    else:
        current = dict(current)  # 不就地改動載入的物件

    if action == "add":
        previous = current.get(user_id)
        if isinstance(previous, dict):
            # 已存在時只更新名稱，保留原始的 added/reason
            merged = dict(previous)
            if name:
                merged["username"] = name
            merged.setdefault("username", "")
            current[user_id] = merged
        else:
            current[user_id] = {
                "added": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "username": name,
                "reason": "manual",
            }
    else:
        current.pop(user_id, None)

    cfg[list_type] = current
    config_module.save_config(cfg)  # 其餘欄位（含 daemon 剛寫的）原樣保留


# ──────────────────────────── 單頁 HTML ────────────────────────────

_CSS = """
:root{
  --bg:#0b0e13; --panel:#12171f; --line:#1f2833; --text:#e6edf3; --muted:#8b98a5;
  --accent:#4ade80; --danger:#f87171; --dim:#243040;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-size:13px;line-height:1.5;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
h1{font-size:15px;margin:0;letter-spacing:.14em;text-transform:uppercase}
h2{font-size:11px;margin:0 0 10px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;font-weight:600}
h3{font-size:12px;margin:0 0 8px;color:var(--text);letter-spacing:.04em}
header{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:baseline;justify-content:space-between;
  padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
.acct{color:var(--muted);font-size:12px;word-break:break-all}
main{padding:18px 20px 40px;max-width:1100px;margin:0 auto;display:grid;gap:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .k{color:var(--muted);font-size:10px;letter-spacing:.09em;text-transform:uppercase}
.card .v{font-size:23px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:13px 15px}
.bars{display:flex;align-items:flex-end;gap:5px;height:132px}
.bar{flex:1;min-width:0;display:flex;flex-direction:column;justify-content:flex-end;gap:3px;text-align:center}
.bar .n{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.bar .fill{background:linear-gradient(180deg,var(--accent),#15803d);border-radius:2px 2px 0 0;min-height:2px}
.bar .d{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden}
.hours{display:flex;align-items:flex-end;gap:2px;height:64px}
.hours .h{flex:1;background:var(--dim);border-top:2px solid var(--accent);min-height:2px}
.axis{display:flex;gap:2px;color:var(--muted);font-size:10px;margin-top:4px;font-variant-numeric:tabular-nums}
.axis span{flex:1;text-align:center}
table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:10px;letter-spacing:.06em;text-transform:uppercase}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
button{font:inherit;background:#1b2430;color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
input{font:inherit;background:#0e131a;color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:5px 8px;min-width:0}
input:focus{outline:none;border-color:var(--accent)}
.lists{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:18px}
.row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.row input[name=user_id]{flex:0 1 110px}
.row input[name=name]{flex:1 1 100px}
.empty{color:var(--muted)}
#msg{min-height:18px;color:var(--accent);font-size:12px}
@media (max-width:620px){main{padding:12px}.bars{height:104px}}
"""

_JS = """
(function(){
  "use strict";
  var msgEl = document.getElementById("msg");
  function msg(text, bad){
    msgEl.textContent = text || "";
    msgEl.style.color = bad ? "#f87171" : "#4ade80";
  }
  function cell(row, text, cls){
    var td = document.createElement("td");
    if(cls){ td.className = cls; }
    td.textContent = text;
    td.title = text;
    row.appendChild(td);
    return td;
  }
  function fill(prefix, listName, items){
    var tbody = document.getElementById(prefix + "-body");
    var ids = Object.keys(items || {});
    ids.sort(function(a, b){ return Number(a) - Number(b); });
    tbody.textContent = "";
    document.getElementById(prefix + "-count").textContent = "(" + ids.length + ")";
    if(!ids.length){
      var tr0 = document.createElement("tr");
      var td0 = document.createElement("td");
      td0.className = "empty";
      td0.textContent = "（空）";
      tr0.appendChild(td0);
      tbody.appendChild(tr0);
      return;
    }
    ids.forEach(function(id){
      var tr = document.createElement("tr");
      cell(tr, id, "num");
      cell(tr, items[id] || "");
      var td = document.createElement("td");
      td.className = "num";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "移除";
      btn.addEventListener("click", function(){ change(listName, "remove", id, ""); });
      td.appendChild(btn);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }
  function loadLists(){
    fetch("/api/lists", {cache: "no-store"}).then(function(res){
      if(!res.ok){ throw new Error(String(res.status)); }
      return res.json();
    }).then(function(data){
      fill("wl", "whitelist", data.whitelist);
      fill("bl", "blacklist", data.blacklist);
    }).catch(function(){ msg("讀取名單失敗", true); });
  }
  function change(listName, action, userId, name){
    fetch("/api/lists", {
      method: "POST",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({list: listName, action: action, user_id: userId, name: name || ""})
    }).then(function(res){
      return res.json().catch(function(){ return {}; }).then(function(data){
        if(res.ok && data.ok){
          msg(action === "add" ? "已加入 " + userId : "已移除 " + userId, false);
          loadLists();
        } else {
          msg("失敗：" + (data.error || res.status), true);
        }
      });
    }).catch(function(){ msg("請求失敗", true); });
  }
  document.querySelectorAll("form[data-list]").forEach(function(form){
    form.addEventListener("submit", function(ev){
      ev.preventDefault();
      var listName = form.getAttribute("data-list");
      var idInput = form.elements["user_id"];
      var nameInput = form.elements["name"];
      var uid = idInput.value.trim();
      if(!/^[0-9]+$/.test(uid)){ msg("使用者 ID 必須是數字", true); return; }
      change(listName, "add", uid, nameInput.value.trim());
      form.reset();
    });
  });
  loadLists();
  setInterval(loadLists, 30000);
})();
"""


def _esc(value) -> str:
    """HTML 轉義（名稱／原因都是外部輸入，一律轉義）。"""
    return html.escape(_as_text(value, _REASON_LIMIT), quote=True)


def _hours_block(by_hour: list) -> str:
    """24 小時直方圖（標題帶次數）。"""
    top = max(by_hour) or 1
    parts = []
    for hour, count in enumerate(by_hour):
        height = 4 if not count else max(6, round(count / top * 100))
        parts.append(
            f'<div class="h" style="height:{height}%" title="{hour:02d}:00 UTC — {count} 次"></div>'
        )
    axis = "".join(f"<span>{h:02d}</span>" for h in (0, 6, 12, 18))
    return f'<div class="hours">{"".join(parts)}</div><div class="axis">{axis}</div>'


def _day_bars(by_day: list) -> str:
    """每日直方圖（缺資料的天由 build_stats 補 0）。"""
    top = max((d["count"] for d in by_day), default=0) or 1
    parts = []
    for item in by_day:
        count = item["count"]
        height = 3 if not count else max(8, round(count / top * 100))
        date = item["date"]
        parts.append(
            '<div class="bar" title="{d} — {c} 次">'
            '<div class="n">{c}</div>'
            '<div class="fill" style="height:{h}%"></div>'
            '<div class="d">{d5}</div>'
            "</div>".format(d=_esc(date), c=count, h=height, d5=_esc(date[5:]))
        )
    if not parts:
        return '<p class="empty">（無資料）</p>'
    return f'<div class="bars">{"".join(parts)}</div>'


def render_html(stats: dict) -> str:
    """把 :func:`build_stats` 的結果渲染成單頁儀表板 HTML。

    所有外部字串（名稱、原因、時間）都經 :func:`html.escape`，
    頁面不含任何 CDN／外部字型／外部圖片。
    """
    stats = stats if isinstance(stats, dict) else {}
    totals = stats.get("totals") if isinstance(stats.get("totals"), dict) else {}
    account = stats.get("account") if isinstance(stats.get("account"), dict) else {}
    lists = stats.get("lists") if isinstance(stats.get("lists"), dict) else {}
    by_source = stats.get("by_source") if isinstance(stats.get("by_source"), dict) else {}
    by_day = stats.get("by_day") if isinstance(stats.get("by_day"), list) else []
    by_hour = stats.get("by_hour") if isinstance(stats.get("by_hour"), list) else [0] * 24
    if len(by_hour) != 24:
        by_hour = list(by_hour)[:24] + [0] * (24 - len(by_hour))
    top_reasons = stats.get("top_reasons") if isinstance(stats.get("top_reasons"), list) else []
    recent = stats.get("recent") if isinstance(stats.get("recent"), list) else []

    cards = [
        ("今日封鎖", totals.get("today", 0)),
        ("近 7 天", totals.get("week", 0)),
        ("近 30 天", totals.get("month", 0)),
        ("日誌筆數", totals.get("blocks", 0)),
        ("累計封鎖", totals.get("all_time", 0)),
        ("群組踢除", totals.get("kicked", 0)),
        ("白名單", lists.get("whitelist", 0)),
        ("黑名單", lists.get("blacklist", 0)),
        ("學習關鍵詞", lists.get("learned_keywords", 0)),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{_esc(k)}</div><div class="v">{int(v or 0)}</div></div>'
        for k, v in cards
    )

    source_html = "".join(
        f"<tr><td>{_esc(name)}</td><td class='num'>{int(count or 0)}</td></tr>"
        for name, count in by_source.items()
    ) or '<tr><td colspan="2" class="empty">（無資料）</td></tr>'

    reason_rows = "".join(
        f"<tr><td title=\"{_esc(item.get('reason'))}\">{_esc(item.get('reason'))}</td>"
        f"<td class='num'>{int(item.get('count') or 0)}</td></tr>"
        for item in top_reasons
        if isinstance(item, dict)
    ) or '<tr><td colspan="2" class="empty">（無資料）</td></tr>'

    recent_rows = ""
    for item in recent:
        if not isinstance(item, dict):
            continue
        stamp = _as_text(item.get("time"), 64)
        recent_rows += (
            "<tr>"
            f"<td title=\"{_esc(stamp)}\">{_esc(stamp[:19].replace('T', ' '))}</td>"
            f"<td class='num'>{_esc(item.get('user_id'))}</td>"
            f"<td>{_esc(item.get('name'))}</td>"
            f"<td>{_esc(item.get('source'))}</td>"
            f"<td title=\"{_esc(item.get('reason'))}\">{_esc(item.get('reason'))}</td>"
            "</tr>"
        )
    if not recent_rows:
        recent_rows = '<tr><td colspan="5" class="empty">（尚無封鎖記錄）</td></tr>'

    title = "TeleShield 儀表板"
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>TeleShield</h1>
  <div class="acct">
    帳號 {_esc(account.get("username") or "（未設定）")} ·
    ID {_esc(account.get("user_id") or "?")} ·
    最後掃描 {_esc(account.get("last_scan") or "從未")}
  </div>
  <button type="button" onclick="location.reload()">重新整理</button>
</header>
<main>
  <section class="cards">{card_html}</section>

  <section class="panel">
    <h2>每日封鎖（近 {len(by_day)} 天，UTC）</h2>
    {_day_bars(by_day)}
  </section>

  <section class="panel">
    <h2>時段分佈（UTC）</h2>
    {_hours_block(by_hour)}
  </section>

  <section class="panel">
    <h2>封鎖來源</h2>
    <table><thead><tr><th>來源</th><th class="num">次數</th></tr></thead>
    <tbody>{source_html}</tbody></table>
  </section>

  <section class="panel">
    <h2>常見原因（前 10）</h2>
    <table><thead><tr><th>原因</th><th class="num">次數</th></tr></thead>
    <tbody>{reason_rows}</tbody></table>
  </section>

  <section class="panel">
    <h2>最近封鎖（最新 20 筆）</h2>
    <table>
      <colgroup><col style="width:140px"><col style="width:90px"><col><col style="width:80px"><col></colgroup>
      <thead><tr><th>時間 (UTC)</th><th class="num">ID</th><th>名稱</th><th>來源</th><th>原因</th></tr></thead>
      <tbody>{recent_rows}</tbody>
    </table>
  </section>

  <section class="panel">
    <h2>名單管理</h2>
    <div class="lists">
      <div>
        <h3>白名單 <span id="wl-count">(0)</span></h3>
        <form class="row" data-list="whitelist">
          <input name="user_id" inputmode="numeric" autocomplete="off" placeholder="使用者 ID">
          <input name="name" autocomplete="off" placeholder="名稱（可選）">
          <button type="submit">加入白名單</button>
        </form>
        <table><thead><tr><th class="num">ID</th><th>名稱</th><th class="num">操作</th></tr></thead>
        <tbody id="wl-body"><tr><td colspan="3" class="empty">（載入中…）</td></tr></tbody></table>
      </div>
      <div>
        <h3>黑名單 <span id="bl-count">(0)</span></h3>
        <form class="row" data-list="blacklist">
          <input name="user_id" inputmode="numeric" autocomplete="off" placeholder="使用者 ID">
          <input name="name" autocomplete="off" placeholder="名稱（可選）">
          <button type="submit">加入黑名單</button>
        </form>
        <table><thead><tr><th class="num">ID</th><th>名稱</th><th class="num">操作</th></tr></thead>
        <tbody id="bl-body"><tr><td colspan="3" class="empty">（載入中…）</td></tr></tbody></table>
      </div>
    </div>
    <p id="msg" role="status"></p>
    <noscript><p class="empty">名單管理需要 JavaScript；統計資料在無 JS 時仍可閱讀。</p></noscript>
  </section>
</main>
<script>{_JS}</script>
</body>
</html>
"""


# ──────────────────────────── HTTP ────────────────────────────


class DashboardHandler(BaseHTTPRequestHandler):
    """儀表板的 HTTP 處理器（僅支援 GET/POST，未知路徑 404）。"""

    server_version = "TeleShieldDashboard/1.0"
    sys_version = ""

    # ── 回應工具 ──
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except OSError:  # 客戶端斷線（BrokenPipe/ConnectionReset）不該噴 traceback
            pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, status: int, error: str) -> None:
        self._send_json(status, {"ok": False, "error": error})

    # ── 路由 ──
    def _path(self) -> str:
        return urlparse(self.path).path

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的命名約定
        path = self._path()
        if path in ("/", "/index.html"):
            page = render_html(build_stats())
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/stats":
            self._send_json(200, build_stats())
            return
        if path == "/api/lists":
            cfg = config_module.load_config()
            if not isinstance(cfg, dict):
                cfg = {}
            self._send_json(
                200,
                {
                    "whitelist": _public_list(cfg, "whitelist"),
                    "blacklist": _public_list(cfg, "blacklist"),
                },
            )
            return
        self._fail(404, "找不到路徑")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self._path() != "/api/lists":
            self._fail(404, "找不到路徑")
            return
        payload = self._read_json_body()
        if payload is None:
            return  # _read_json_body 已經回過錯誤
        try:
            change = validate_change(payload)
        except ValueError as exc:
            self._fail(400, str(exc))
            return
        try:
            apply_change(**change)
        except OSError as exc:
            _log.warning("寫入 config 失敗: %s", exc)
            self._fail(500, "寫入設定檔失敗")
            return
        self._send_json(200, {"ok": True})

    def _read_json_body(self) -> Optional[dict]:
        """驗證 Content-Length 上限並解析 JSON；失敗時已回應並回 ``None``。"""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._fail(400, "缺少 Content-Length")
            return None
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._fail(400, "Content-Length 不合法")
            return None
        if length <= 0:
            self._fail(400, "請求內容必須是 JSON 物件")
            return None
        if length > MAX_BODY_BYTES:
            self._fail(400, f"請求內容過大（上限 {MAX_BODY_BYTES} 位元組）")
            return None
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._fail(400, "請求內容不是合法 JSON")
            return None
        if not isinstance(payload, dict):
            self._fail(400, "請求內容必須是 JSON 物件")
            return None
        return payload

    def log_message(self, fmt, *args) -> None:  # noqa: A003 — 覆寫父類方法
        """改走 logging（預設會直接寫 stderr，對常駐程式太吵）。"""
        _log.debug("%s - %s", self.address_string(), fmt % args)


def create_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """建立並 bind/listen 一個儀表板伺服器（尚未啟動事件迴圈）。"""
    if host not in _LOCAL_HOSTS:
        print(
            f"⚠️  綁定非本機位址 {host}：儀表板將對外網開放，請自行確認網路與存取控制。",
            file=sys.stderr,
        )
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.daemon_threads = True
    return server


def _write_ready_file(path: Path, url: str, host: str, port: int) -> None:
    """原子寫入就緒檔（測試先看到檔案、再讀內容，不會讀到半截）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"url": url, "host": host, "port": port, "pid": os.getpid()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def _display_host(host: str) -> str:
    """``0.0.0.0``/``::`` 這種萬用位址在 URL 裡要換成可連的本機位址。"""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    open_browser: bool = False,
    ready_file: Optional[Path] = None,
) -> None:
    """啟動儀表板並阻塞（``Ctrl-C`` 乾淨收掉）。

    ``ready_file`` 若給了，**在真正 listen 之後**才把實際位址（JSON：
    ``{url, host, port, pid}``）寫進去，供自動化測試等待後再打 API；離開時刪除。
    ``port=0`` 由系統挑選可用埠（測試用）。
    """
    server = create_server(host, port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{_display_host(bound_host)}:{bound_port}/"

    with _SERVERS_LOCK:
        _ACTIVE_SERVERS.append(server)

    ready_path = Path(ready_file) if ready_file is not None else None
    try:
        if ready_path is not None:
            _write_ready_file(ready_path, url, bound_host, bound_port)
        print(f"📊 TeleShield 儀表板：{url}（Ctrl-C 結束）", file=sys.stderr)
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n👋 儀表板已停止", file=sys.stderr)
    finally:
        with _SERVERS_LOCK:
            if server in _ACTIVE_SERVERS:
                _ACTIVE_SERVERS.remove(server)
        try:
            server.server_close()
        finally:
            if ready_path is not None:
                try:
                    ready_path.unlink()
                except OSError:
                    pass


def shutdown_active_servers() -> None:
    """關閉所有由 :func:`serve` 啟動中的伺服器（供測試與程式內嵌使用）。"""
    with _SERVERS_LOCK:
        servers = list(_ACTIVE_SERVERS)
    for server in servers:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 — 已經關掉的不影響其他伺服器
            _log.debug("關閉伺服器失敗", exc_info=True)


def main(argv=None) -> int:
    """``python -m teleshield.dashboard`` 入口（零依賴 argparse）。"""
    parser = argparse.ArgumentParser(
        prog="teleshield-dashboard",
        description="TeleShield 本機網頁儀表板（標準庫，預設只綁 127.0.0.1）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="綁定位址（預設 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8787, help="連接埠，0 表示由系統挑選")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="明確允許對外綁定 0.0.0.0（僅在可信網路下使用）",
    )
    parser.add_argument("--open", action="store_true", help="啟動後開啟瀏覽器")
    parser.add_argument("--ready-file", default=None, help="啟動後把實際位址寫入此檔案")
    args = parser.parse_args(argv)

    host = args.host
    if args.allow_remote and host in ("127.0.0.1", "localhost"):
        host = "0.0.0.0"
    if host not in _LOCAL_HOSTS and not args.allow_remote:
        print(
            f"❌ 拒絕綁定非本機位址 {host}：請加上 --allow-remote 明確授權。",
            file=sys.stderr,
        )
        return 2

    serve(
        host,
        args.port,
        open_browser=args.open,
        ready_file=Path(args.ready_file) if args.ready_file else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
