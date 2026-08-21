"""TeleShield 核心動作：設定、掃描、群組掃描、即時監聽、名單管理、報告、學習。"""

from __future__ import annotations

import asyncio
import re as _re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from telethon import events
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.functions.contacts import BlockRequest, GetContactsRequest
from telethon.tl.types import Channel, Chat, ChatBannedRights, User

from .behavior import BehaviorTracker
from .client import get_client
from .config import (
    HOME_DIR,
    is_blacklisted,
    is_whitelisted,
    load_block_log,
    load_config,
    load_learned_patterns,
    log_block,
    save_config,
    save_learned_patterns,
)
from .ocr import check_photo
from .patterns import SPAM_PATTERNS, learn_from_text
from .scoring import SpamScorer, Verdict

__all__ = [
    "setup",
    "scan_and_block",
    "scan_groups",
    "listen",
    "manage_list",
    "report",
    "learn",
]


# ──────────── 首次設定 ────────────

async def setup(api_id: str = None, api_hash: str = None, phone: str = None, code: str = None) -> bool:
    print("\n═══════════════════════════════")
    print("  TeleShield - 設定")
    print("═══════════════════════════════\n")

    if not api_id:
        api_id = input("API ID (從 my.telegram.org/apps 取得): ").strip()
    else:
        print(f"API ID: {api_id}")
    if not api_hash:
        api_hash = input("API Hash: ").strip()
    else:
        print(f"API Hash: {api_hash}")
    if not phone:
        phone = input("手機號碼 (含國碼，如 +852****5931): ").strip()
    else:
        print("手機隱藏")

    HOME_DIR.mkdir(parents=True, exist_ok=True)
    client = get_client({"api_id": int(api_id), "api_hash": api_hash})

    try:
        await client.start(phone=phone, code_callback=lambda: code or input("請輸入驗證碼: "))
        me = await client.get_me()
        print("\n✅ 登入成功！")
        print(f"   帳號: {me.first_name} (@{me.username or '無'})")
        print(f"   ID: {me.id}")

        save_config({
            "api_id": int(api_id),
            "api_hash": api_hash,
            "phone": phone,
            "user_id": me.id,
            "username": me.username,
            "blocked_count": 0,
            "kicked_count": 0,
            "last_scan": None,
            "whitelist": {},
            "blacklist": {},
            "managed_groups": [],
            "learned_patterns": {"keywords": [], "patterns": []},
            "listen_scan_groups": True,
        })
        print("✅ 設定已儲存")
        await client.disconnect()
        return True
    except Exception as e:
        print(f"\n❌ 登入失敗: {e}")
        return False


# ──────────── 掃描私訊封鎖 ────────────

async def _scan_text(client, msg, cfg, now, days=14, scorer=None):
    """檢查單條消息是否廣告。返回 (spam_text, is_ocr, result) 或 None。"""
    scorer = scorer or SpamScorer(cfg)
    if not msg:
        return None
    if msg.date and msg.date < now - timedelta(days=days):
        return None
    msg_text = msg.text or ""
    res = scorer.score(msg_text)
    if res.verdict != Verdict.PASS:
        return msg_text[:120], False, res
    if msg.photo:
        ocr_text = await check_photo(client, msg)
        if ocr_text:
            res2 = scorer.score(ocr_text)
            if res2.verdict != Verdict.PASS:
                return f"[OCR] {ocr_text[:100]}", True, res2
    return None


async def scan_and_block(dry_run: bool = False):
    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定，請先執行 --setup")
        return

    print(f"{'🧪 試運行' if dry_run else '🔍 掃描模式'}")
    print(f"{'─'*40}")

    client = get_client(cfg)
    try:
        await client.start(phone=cfg["phone"])

        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {c.id for c in contacts}
        print(f"📇 聯絡人: {len(contact_ids)} 位")

        now = datetime.now(timezone.utc)
        dialogs = await client.get_dialogs(limit=30)

        blocked = 0
        skipped = 0
        flagged = 0
        scorer = SpamScorer(cfg)

        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, User) or entity.is_self or entity.id in contact_ids or entity.bot:
                continue

            try:
                msgs = await client.get_messages(entity, limit=5)
            except Exception:
                continue

            found = None
            for msg in msgs:
                found = await _scan_text(client, msg, cfg, now, scorer=scorer)
                if found:
                    break
            if not found:
                continue

            spam_text = found[0]
            result = found[2]
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            uname = f"@{entity.username}" if entity.username else ""
            print(f"\n  ⚠️  廣告: {name} {uname}")
            print(f"      {spam_text[:120]}")

            # FLAG：只記錄標記，不封鎖（用戶觀察）
            if result.verdict == Verdict.FLAG:
                flagged += 1
                log_block(entity.id, name, spam_text, "flag")
                print(f"      🏷️  可疑（分數 {result.score}）— 已標記，未封鎖")
                continue

            if dry_run:
                skipped += 1
                continue

            try:
                await client(BlockRequest(id=entity.id))
                blocked += 1
                log_block(entity.id, name, spam_text, "scan")
                print(f"      ✅ 封鎖（分數 {result.score}）")
            except Exception as e:
                print(f"      ❌ 失敗: {e}")

        print(f"\n{'─'*40}")
        print(f"結果: 已處理 {blocked+skipped+flagged}（封鎖 {blocked} / 標記 {flagged} / 試運行 {skipped}）")
        if not dry_run and blocked > 0:
            cfg["blocked_count"] = cfg.get("blocked_count", 0) + blocked
        cfg["flagged_count"] = cfg.get("flagged_count", 0) + flagged
        cfg["last_scan"] = now.isoformat()
        save_config(cfg)
        await client.disconnect()
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        await client.disconnect()


# ──────────── 掃描群組踢除 ────────────

async def scan_groups(dry_run: bool = False):
    """掃描群組訊息，踢除發廣告的成員"""
    from telethon.errors import UserAdminInvalidError

    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定")
        return

    print(f"{'🧪 試運行' if dry_run else '👥 群組掃描模式'}")
    print(f"{'─'*40}")

    client = get_client(cfg)
    try:
        await client.start(phone=cfg["phone"])
        me = await client.get_me()
        now = datetime.now(timezone.utc)

        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {c.id for c in contacts}

        dialogs = await client.get_dialogs(limit=50)
        groups = []
        for d in dialogs:
            if isinstance(d.entity, (Chat, Channel)) and not d.entity.broadcast:
                try:
                    participant = await client.get_permissions(d.entity, me.id)
                    if participant and participant.is_admin:
                        groups.append(d)
                except Exception:
                    pass

        if not groups:
            print("⚠️  沒有可管理的群組（需要是管理員）")
            await client.disconnect()
            return

        print(f"👥 管理中的群組: {len(groups)}")
        kicked = 0
        total_scanned = 0
        flagged = 0
        scorer = SpamScorer(cfg)

        for dialog in groups:
            entity = dialog.entity
            title = getattr(entity, "title", "未知群組")
            try:
                msgs = await client.get_messages(entity, limit=20)
            except Exception:
                continue

            for msg in msgs:
                if not msg or not msg.sender_id:
                    continue
                if msg.sender_id == me.id:
                    continue
                if msg.sender_id in contact_ids:
                    continue
                if is_whitelisted(msg.sender_id, cfg):
                    continue
                if msg.date and msg.date < now - timedelta(days=3):
                    continue

                found = await _scan_text(client, msg, cfg, now, days=3, scorer=scorer)
                if not found:
                    continue
                spam_reason = found[0]
                result = found[2]

                total_scanned += 1
                try:
                    sender = await client.get_entity(msg.sender_id)
                    sname = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                except Exception:
                    sname = str(msg.sender_id)

                print(f"\n  ⚠️  [{title}] {sname}")
                print(f"     {spam_reason[:100]}")

                # FLAG：只記錄標記
                if result.verdict == Verdict.FLAG:
                    flagged += 1
                    log_block(msg.sender_id, sname, spam_reason, "flag")
                    print(f"     🏷️  可疑（分數 {result.score}）— 已標記，未踢除")
                    continue

                if dry_run:
                    continue

                try:
                    rights = ChatBannedRights(until_date=None, view_messages=True)
                    await client(EditBannedRequest(entity, msg.sender_id, rights))
                    kicked += 1
                    log_block(msg.sender_id, sname, spam_reason, "group")
                    print("     ✅ 已踢除")
                    await asyncio.sleep(1)
                except UserAdminInvalidError:
                    print("     ⚠️ 無法踢除（權限不足）")
                except Exception as e:
                    print(f"     ❌ 踢除失敗: {e}")

        print(f"\n{'─'*40}")
        print(f"結果: 掃描 {total_scanned} 條, {'已踢除' if not dry_run else '試運行'}: {kicked if not dry_run else total_scanned}（標記 {flagged}）")
        if not dry_run and kicked > 0:
            cfg["kicked_count"] = cfg.get("kicked_count", 0) + kicked
        cfg["flagged_count"] = cfg.get("flagged_count", 0) + flagged
        save_config(cfg)
        await client.disconnect()
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        await client.disconnect()


# ──────────── 即時監聽（私訊+群組） ────────────

async def listen():
    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定")
        return

    print("👂 TeleShield 即時監聽啟動中...")
    print("    ✅ 私訊廣告 → 自動封鎖")
    print("    👥 群組廣告 → 自動踢除（管理員身份）")
    print("    🔍 群組行為分析 → 進群秒發連結/刷屏廣告自動踢除")
    print("    📸 OCR 支援 → 純圖片廣告也辨識")
    print("    按 Ctrl+C 停止\n")

    client = get_client(cfg)
    scorer = SpamScorer(cfg)
    tracker = BehaviorTracker()

    # 入群事件：記錄新成員加入時間（行為分析用）
    @client.on(events.ChatAction)
    async def on_join(event):
        if not event.user_added and event.user_id != event.chat_id:
            return
        added = event.user_ids or (event.user_id and [event.user_id]) or []
        for uid in added:
            if uid and uid != (await client.get_me()).id:
                tracker.record_join(event.chat_id, uid)

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        msg = event.message
        if not msg or not msg.sender_id:
            return

        sender_id = msg.sender_id
        chat = await event.get_chat()
        sender = await event.get_sender()

        if hasattr(sender, 'is_self') and sender.is_self:
            return

        # 群組消息：先記錄行為（供爆發/新成員分析）
        if isinstance(chat, (Chat, Channel)):
            tracker.record_message(chat.id, sender_id, msg.text or "")

        if is_blacklisted(sender_id, cfg):
            try:
                if isinstance(chat, (Chat, Channel)):
                    rights = ChatBannedRights(until_date=None, view_messages=True)
                    await client(EditBannedRequest(chat, sender_id, rights))
                else:
                    await client(BlockRequest(id=sender_id))
            except Exception:
                pass
            return

        if is_whitelisted(sender_id, cfg):
            return

        if isinstance(chat, User):
            sender = chat
            if sender_id == cfg.get("user_id"):
                return
            if sender.bot:
                return

            try:
                contacts = (await client(GetContactsRequest(hash=0))).users
                contact_ids = {c.id for c in contacts}
                if sender_id in contact_ids:
                    return
            except Exception:
                pass

            spam_text = msg.text or ""
            # 帳號特徵（weak profile 信號）
            user_info = {
                "username": getattr(sender, "username", None),
                "photo": None,
                "bio": None,
            }
            result = scorer.score(spam_text, user_info=user_info)
            ocr_found_spam = False
            if result.verdict == Verdict.PASS and msg.photo:
                ocr_text = await check_photo(client, msg)
                if ocr_text:
                    result = scorer.score(ocr_text)
                    if result.verdict != Verdict.PASS:
                        ocr_found_spam = True
                        spam_text = ocr_text[:100]

            if result.verdict == Verdict.PASS:
                return

            name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
            uname = f"@{sender.username}" if sender.username else ""
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            icon = "📸" if ocr_found_spam else ""
            print(f"\n[{ts}] {icon}⚠️  私訊廣告: {name} {uname}")
            print(f"    {spam_text[:100]}")

            # FLAG：只標記不封鎖
            if result.verdict == Verdict.FLAG:
                log_block(sender_id, name, spam_text, "flag")
                cfg["flagged_count"] = cfg.get("flagged_count", 0) + 1
                save_config(cfg)
                print(f"     🏷️  可疑（分數 {result.score}）— 已標記，未封鎖")
                return

            try:
                await client(BlockRequest(id=sender_id))
                cfg["blocked_count"] = cfg.get("blocked_count", 0) + 1
                save_config(cfg)
                log_block(sender_id, name, spam_text, "private")
                print(f"     ✅ 封鎖（分數 {result.score}）（累計 {cfg['blocked_count']}）")
            except Exception as e:
                print(f"     ❌ 封鎖失敗: {e}")
            return

        if isinstance(chat, (Chat, Channel)) and not chat.broadcast:
            try:
                me = await client.get_me()
                perm = await client.get_permissions(chat, me.id)
                if not perm or not perm.is_admin:
                    return
            except Exception:
                return

            try:
                s_perm = await client.get_permissions(chat, sender_id)
                if s_perm and (s_perm.is_admin or s_perm.is_creator):
                    return
            except Exception:
                pass

            sname = f"{sender.first_name or ''} {sender.last_name or ''}".strip() if hasattr(sender, 'first_name') else str(sender_id)
            title = getattr(chat, "title", "群組")

            # 行為分析：進群秒發連結 / 刷屏廣告 → 直接踢除
            suspicious, behavior_reason = tracker.suspicious(chat.id, sender_id)
            if suspicious:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\n[{ts}] 🔍 行為可疑 [{title}]: {sname}")
                print(f"    {behavior_reason}")
                try:
                    rights = ChatBannedRights(until_date=None, view_messages=True)
                    await client(EditBannedRequest(chat, sender_id, rights))
                    cfg["kicked_count"] = cfg.get("kicked_count", 0) + 1
                    save_config(cfg)
                    log_block(sender_id, sname, f"[behavior] {behavior_reason}", "group")
                    print(f"     ✅ 已踢除（行為分析）（累計 {cfg['kicked_count']}）")
                except Exception as e:
                    print(f"     ❌ 踢除失敗: {e}")
                return

            msg_text = msg.text or ""
            result = scorer.score(msg_text)
            spam_reason = ""
            if result.verdict != Verdict.PASS:
                spam_reason = msg_text[:100]
            elif msg.photo:
                ocr_text = await check_photo(client, msg)
                if ocr_text:
                    result = scorer.score(ocr_text)
                    if result.verdict != Verdict.PASS:
                        spam_reason = f"[OCR] {ocr_text[:80]}"

            if not spam_reason:
                return

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[{ts}] 👥 群組廣告 [{title}]: {sname}")
            print(f"    {spam_reason[:100]}")

            # FLAG：只標記不踢除
            if result.verdict == Verdict.FLAG:
                log_block(sender_id, sname, spam_reason, "flag")
                cfg["flagged_count"] = cfg.get("flagged_count", 0) + 1
                save_config(cfg)
                print(f"     🏷️  可疑（分數 {result.score}）— 已標記，未踢除")
                return

            try:
                rights = ChatBannedRights(until_date=None, view_messages=True)
                await client(EditBannedRequest(chat, sender_id, rights))
                cfg["kicked_count"] = cfg.get("kicked_count", 0) + 1
                save_config(cfg)
                log_block(sender_id, sname, spam_reason, "group")
                print(f"     ✅ 已踢除（分數 {result.score}）（累計 {cfg['kicked_count']}）")
            except Exception as e:
                print(f"     ❌ 踢除失敗: {e}")

    try:
        await client.start(phone=cfg["phone"])
        print("✅ TeleShield 已上線 — 監聽中...")
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\n\n👋 已停止")
        await client.disconnect()
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        await client.disconnect()


# ──────────── 白名單/黑名單管理 ────────────

async def manage_list(action: str, list_type: str, user_id_str: str = None):
    cfg = load_config()
    # 修復：config 中名單鍵為 "blacklist"/"whitelist"（舊版誤存 "xxx_list" 已不可讀）
    key = list_type if list_type in ("blacklist", "whitelist") else f"{list_type}_list"
    lst = cfg.get(key, {})

    # 遷移：舊版誤存的 xxx_list 併入
    legacy = cfg.get(f"{list_type}_list")
    if legacy and not lst:
        lst = legacy
        cfg.pop(f"{list_type}_list", None)

    if action == "list":
        if not lst:
            print(f"📋 {list_type} 名單: 空")
        else:
            print(f"📋 {list_type} 名單 ({len(lst)} 人):")
            for uid, info in sorted(lst.items()):
                tag = f"@{info.get('username','')}" if info.get('username') else ""
                print(f"  • {uid} {tag} ({info.get('added','?')})")
        return

    # import / export：community 名單交換（JSON id 列表）
    if action == "export":
        import json as _json

        if not user_id_str:
            print("❌ 用法: --blacklist export <file.json>")
            return
        try:
            with open(user_id_str, "w", encoding="utf-8") as f:
                _json.dump({"users": list(lst.keys()), "type": list_type, "version": 1}, f, ensure_ascii=False, indent=2)
            print(f"✅ 已導出 {len(lst)} 人到 {user_id_str}")
        except OSError as e:
            print(f"❌ 導出失敗: {e}")
        return

    if action == "import":
        import json as _json

        if not user_id_str:
            print("❌ 用法: --blacklist import <file.json>")
            return
        try:
            with open(user_id_str, encoding="utf-8") as f:
                data = _json.load(f)
            ids = data.get("users", []) if isinstance(data, dict) else data
            if not isinstance(ids, list):
                print("❌ 檔案格式不正確（需 {\"users\": [\"123\", ...]}）")
                return
        except (OSError, ValueError) as e:
            print(f"❌ 讀取失敗: {e}")
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        added = 0
        for uid in ids:
            uid = str(uid)
            if uid not in lst:
                lst[uid] = {"added": now, "username": "", "reason": "community"}
                added += 1
        cfg[key] = lst
        save_config(cfg)
        print(f"✅ 已導入 {added} 人（跳過 {len(ids)-added} 已存在），{list_type} 共 {len(lst)} 人")
        return

    if not user_id_str:
        print("❌ 請提供使用者 ID")
        return

    user_id = user_id_str
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if action == "add":
        lst[user_id] = {"added": now, "username": "", "reason": "manual"}
        cfg[key] = lst
        save_config(cfg)
        print(f"✅ 已將 {user_id} 加入 {list_type} 名單")
    elif action == "remove":
        if user_id in lst:
            del lst[user_id]
            cfg[key] = lst
            save_config(cfg)
            print(f"✅ 已將 {user_id} 從 {list_type} 名單移除")
        else:
            print(f"❌ {user_id} 不在 {list_type} 名單中")
    else:
        print(f"❌ 未知操作: {action}")


# ──────────── 封鎖摘要報告 ────────────

async def report(period: str = "day", output_html: bool = False):
    log = load_block_log()
    blocks = log.get("blocks", [])
    if not blocks:
        print("📊 尚無封鎖記錄")
        return

    now = datetime.now(timezone.utc)
    if period == "day":
        cutoff = now - timedelta(days=1)
        label = "過去 24 小時"
    elif period == "week":
        cutoff = now - timedelta(days=7)
        label = "過去 7 天"
    else:
        cutoff = datetime.min.replace(tzinfo=timezone.utc)
        label = "全部"

    recent = [b for b in blocks if datetime.fromisoformat(b["time"]) > cutoff]

    if not recent:
        print(f"📊 {label}: 無封鎖記錄")
        return

    total = len(recent)
    sources = defaultdict(int)
    reasons = defaultdict(int)
    for b in recent:
        sources[b.get("source", "private")] += 1
        reason = b.get("reason", "")
        for pat in SPAM_PATTERNS:
            m = re_search(pat, reason)
            if m:
                tag = reason[:16] if len(reason) > 16 else reason
                reasons[tag] += 1
                break
        else:
            reasons[reason[:20]] += 1

    if output_html:
        _render_html_report(recent, total, sources, reasons, period, label)
        return

    print(f"\n📊 封鎖摘要 — {label}")
    print(f"{'─'*40}")
    print(f"   總計封鎖: {total} 人")
    print("")

    if len(sources) > 1:
        print("   來源:")
        for s, c in sorted(sources.items(), key=lambda x: -x[1]):
            label_s = "私訊" if s == "private" else "群組"
            print(f"     • {label_s}: {c} 人")

    print("   廣告類型 Top 5:")
    for r, c in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
        print(f"     • {r}: {c} 次")

    if period == "week":
        days = defaultdict(int)
        for b in recent:
            d = b["time"][:10]
            days[d] += 1
        print("\n   每日趨勢:")
        for d in sorted(days.keys()):
            print(f"     {d}: {days[d]} 人")


def _render_html_report(recent, total, sources, reasons, period, label):
    """生成自包含 HTML 報告（無外部依賴，可直接開/部署）。"""
    import html as _html

    source_map = {"private": "私訊", "group": "群組", "scan": "掃描", "flag": "標記"}
    days = defaultdict(int)
    for b in recent:
        days[b["time"][:10]] += 1

    def _bars(data):
        if not data:
            return "<p class='muted'>無數據</p>"
        mx = max(data.values()) or 1
        out = []
        for k, v in sorted(data.items(), key=lambda x: -x[1])[:10]:
            pct = int(v / mx * 100)
            out.append(
                f"<div class='row'><span class='k'>{_html.escape(str(k)[:24])}</span>"
                f"<div class='bar'><div class='fill' style='width:{pct}%'></div></div>"
                f"<span class='v'>{v}</span></div>"
            )
        return "\n".join(out)

    day_rows = "\n".join(
        f"<div class='row'><span class='k'>{d}</span>"
        f"<div class='bar'><div class='fill' style='width:{int(c/max(days.values())*100)}%'></div></div>"
        f"<span class='v'>{c}</span></div>"
        for d, c in sorted(days.items())
    ) if days else ""

    rows = ""
    for b in recent[-30:]:
        src = source_map.get(b.get("source", ""), b.get("source", ""))
        rows += (
            f"<tr><td class='muted'>{_html.escape(str(b.get('time',''))[5:16])}</td>"
            f"<td>{src}</td><td class='muted'>{_html.escape(str(b.get('user_id','')))}</td>"
            f"<td>{_html.escape(str(b.get('name',''))[:16])}</td>"
            f"<td class='reason'>{_html.escape(str(b.get('reason',''))[:60])}</td></tr>"
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TeleShield 報告 — {_html.escape(label)}</title>
<style>
  :root {{ --bg:#0f1115; --card:#171a21; --fg:#e8e6e3; --muted:#8a8a93; --acc:#ff9e5e; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--fg); font:14px/1.6 -apple-system, "PingFang TC", "Noto Sans TC", sans-serif; padding:32px 16px; }}
  .wrap {{ max-width:720px; margin:0 auto; }}
  h1 {{ font-size:22px; margin-bottom:4px; }}
  .sub {{ color:var(--muted); margin-bottom:24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:24px; }}
  .card {{ background:var(--card); border-radius:12px; padding:16px; }}
  .card .n {{ font-size:26px; font-weight:700; color:var(--acc); }}
  .card .t {{ color:var(--muted); font-size:12px; }}
  .panel {{ background:var(--card); border-radius:12px; padding:16px 20px; margin-bottom:16px; }}
  .panel h2 {{ font-size:14px; color:var(--acc); margin-bottom:12px; }}
  .row {{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }}
  .k {{ width:150px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--fg); }}
  .bar {{ flex:1; background:#23272f; border-radius:4px; height:10px; overflow:hidden; }}
  .fill {{ height:100%; background:linear-gradient(90deg,#ff9e5e,#ff6b3d); border-radius:4px; }}
  .v {{ width:36px; text-align:right; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; color:var(--muted); font-weight:500; font-size:12px; padding:6px 8px; border-bottom:1px solid #23272f; }}
  td {{ padding:6px 8px; border-bottom:1px solid #1c2027; }}
  .muted {{ color:var(--muted); }}
  .reason {{ max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  @media (max-width:600px) {{ .k {{ width:90px; }} .reason {{ max-width:120px; }} }}
</style></head><body><div class="wrap">
<h1>🛡️ TeleShield 報告</h1>
<div class="sub">{_html.escape(label)} · 生成於 {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>
<div class="cards">
  <div class="card"><div class="n">{total}</div><div class="t">處理總數</div></div>
  <div class="card"><div class="n">{sources.get('private',0)}</div><div class="t">私訊封鎖</div></div>
  <div class="card"><div class="n">{sources.get('group',0)}</div><div class="t">群組踢除</div></div>
  <div class="card"><div class="n">{sources.get('flag',0)}</div><div class="t">標記（未動作）</div></div>
</div>
<div class="panel"><h2>廣告類型 Top 10</h2>{_bars(reasons)}</div>
<div class="panel"><h2>每日趨勢</h2>{day_rows}</div>
<div class="panel"><h2>最近 30 筆</h2>
<table><thead><tr><th>時間</th><th>來源</th><th>ID</th><th>用戶</th><th>原因</th></tr></thead>
<tbody>{rows}</tbody></table></div>
</div></body></html>"""

    out = HOME_DIR / f"report_{period}.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"📄 報告已生成: {out}")


# ──────────── 學習模式 ────────────

async def learn(text: str):
    """手動標記廣告文字，自動提取關鍵字和模式"""
    if not text:
        print("❌ 請提供廣告文字")
        return

    cfg = load_config()
    learned = load_learned_patterns()

    new_kws, new_patterns = learn_from_text(text, learned)
    if not new_kws and not new_patterns:
        print("⚠️  未能提取新模式")
        return

    learned["keywords"].extend(new_kws)
    learned["patterns"].extend(new_patterns)
    save_learned_patterns(learned)
    # 向後相容：config.json 也同步一份
    cfg["learned_patterns"] = learned
    save_config(cfg)

    print(f"✅ 已學習 {len(new_kws)} 個關鍵詞 + {len(new_patterns)} 個模式")
    if new_kws:
        print(f"   關鍵詞: {', '.join(new_kws)}")
    if new_patterns:
        print(f"   模式: {', '.join(new_patterns[:5])}")
    print(f"   累計: {len(learned['keywords'])} 關鍵詞, {len(learned['patterns'])} 模式")


def re_search(pat, text):
    """報告模組用的安全搜尋（learned patterns 不影響統計）。"""
    try:
        return _re.search(pat, text)
    except _re.error:
        return None
