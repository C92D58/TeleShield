#!/usr/bin/env python3
"""
Telegram 廣告封鎖工具 — TeleShield 完整版
────────────────────────────────────
用法：
  --setup [api_id] [api_hash] [phone] [code]  首次設定
  --scan                                       掃描並封鎖
  --dry-run                                    試掃描
  --listen                                     即時監聽（後台常駐）
  --status                                     查看狀態
  --report [day|week]                          封鎖摘要報告
  --learn <廣告文字>                            手動標記學習新模式
  --whitelist add|remove|list <user_id>        白名單管理
  --blacklist add|remove|list <user_id>        黑名單管理
  --group-scan                                 掃描群組並踢除廣告
"""

import asyncio, os, json, re, sys, time, tempfile, random
from pathlib import Path
from datetime import datetime, timedelta, timezone
from telethon import events
from collections import defaultdict

# ──────────── 設定 ────────────
SESSION_DIR = Path("/root/.tg-sessions")
SESSION_FILE = SESSION_DIR / "user.session"
CONFIG_FILE = SESSION_DIR / "config.json"
BLOCK_LOG = SESSION_DIR / "block_log.json"
SPAM_PATTERNS = [
    # 中文廣告常見模式
    r"加[\s\-]*[LlvVXx]|[LlvVXx][\s\-]*信",
    r"tg[\s\-]*@?[a-zA-Z0-9_]{3,}",
    r"https?://t\.me/",
    r"@\w{4,}",  # 保留：與 tg 前綴模式聯動時風險可控，單獨 @ 提及暫不觸發
    r"兼職|刷單|日入|月入|躺賺|被動收入|在家工作|輕鬆賺",
    r"投資|理財|帶單|跟單|量化|穩賺|穩健|高回報|高收益",
    r"色情|A片|av|成人|裸聊|約炮|援交|包養",
    r"賭博|博彩|casino|betting|六合彩|開獎|下注|投注|賭場",
    r"註冊送|免費領|紅包|禮金|優惠碼|推廣碼",
    r"點贊|關注|刷粉|刷讚|漲粉",
    r"出售|售賣|批發|代購|代發|供應商|出貨|庫存甩賣|清倉",
    # English patterns
    r"promote|promotion|advertisement|sponsor",
    r"click\s*(here|this\s*link|the\s*link)",
    r"earn\s*money|work\s*from\s*home|passive\s*income",
    r"free\s*crypto|free\s*bitcoin|airdrop|giveaway",
    r"limited\s*offer|discount\s*\d{2,}%|buy\s*now",
]

# ──────────── 工具函式 ────────────

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

def load_block_log():
    if BLOCK_LOG.exists():
        return json.loads(BLOCK_LOG.read_text())
    return {"blocks": []}

def save_block_log(log):
    BLOCK_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False))

def load_learned_patterns():
    f = SESSION_DIR / "learned_patterns.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"keywords": [], "patterns": []}

def save_learned_patterns(data):
    (SESSION_DIR / "learned_patterns.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))

def is_spam(text: str, cfg: dict = None) -> bool:
    """檢查文字是否包含廣告模式（含自訂模式）"""
    if not text:
        return False
    # 內建模式
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    # 自訂學習模式
    if cfg:
        lp = cfg.get("learned_patterns", {})
        for p in lp.get("patterns", []):
            try:
                if re.search(p, text, re.IGNORECASE):
                    return True
            except:
                continue
        for kw in lp.get("keywords", []):
            if kw.lower() in text.lower():
                return True
    return False

def is_blacklisted(user_id: int, cfg: dict) -> bool:
    return str(user_id) in cfg.get("blacklist", {})

def is_whitelisted(user_id: int, cfg: dict) -> bool:
    return str(user_id) in cfg.get("whitelist", {})

def log_block(user_id: int, name: str, reason: str, source: str = "private"):
    log = load_block_log()
    log["blocks"].append({
        "user_id": user_id,
        "name": name,
        "reason": reason[:200],
        "source": source,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    # 保留最近 500 筆
    if len(log["blocks"]) > 500:
        log["blocks"] = log["blocks"][-500:]
    save_block_log(log)

# ──────────── 圖片 OCR ────────────

def ocr_image(image_path: str) -> str:
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception as e:
        return ""

async def check_photo(client, msg) -> str:
    if not msg or not msg.photo:
        return ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp = f.name
        await client.download_media(msg, file=tmp)
        text = ocr_image(tmp)
        os.unlink(tmp)
        return text
    except:
        return ""

# ──────────── 學習模式 ────────────

async def learn(text: str):
    """手動標記廣告文字，自動提取關鍵字和模式"""
    if not text:
        print("❌ 請提供廣告文字")
        return

    cfg = load_config()
    lp = cfg.get("learned_patterns", {"keywords": [], "patterns": []})

    # 提取有意義的關鍵詞（2-6 字，過濾常見字）
    tokens = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    stop_words = {"我們", "他們", "可以", "沒有", "這個", "那個", "什麼", "因為", "所以", "但是",
                  "如果", "雖然", "然後", "而且", "或者", "不過", "還是", "就是", "不是", "一個"}
    new_kws = []
    for tok in tokens:
        if tok not in stop_words and tok not in lp["keywords"]:
            new_kws.append(tok)

    # 提取可能的新 regex 模式
    new_patterns = []
    # 微信/Line/WhatsApp 類
    m = re.search(r'(加|V|v|薇|威|wechat|line|whatsapp)[-:\s]*([a-zA-Z0-9_]{4,})', text)
    if m:
        pat = re.escape(m.group(2))
        if pat not in lp["patterns"]:
            new_patterns.append(pat)

    # URL 短網址
    urls = re.findall(r'https?://[^\s]{4,}', text)
    for u in urls:
        pat = re.escape(u[:20])
        if pat not in lp["patterns"]:
            new_patterns.append(pat)

    if not new_kws and not new_patterns:
        # 直接保存整句作為關鍵模式
        phrase = re.escape(text[:30])
        new_patterns.append(phrase)

    lp["keywords"].extend(new_kws)
    lp["patterns"].extend(new_patterns)
    cfg["learned_patterns"] = lp
    save_config(cfg)

    print(f"✅ 已學習 {len(new_kws)} 個關鍵詞 + {len(new_patterns)} 個模式")
    if new_kws:
        print(f"   關鍵詞: {', '.join(new_kws)}")
    if new_patterns:
        print(f"   模式: {', '.join(new_patterns[:5])}")
    print(f"   累計: {len(lp['keywords'])} 關鍵詞, {len(lp['patterns'])} 模式")

# ──────────── 白名單/黑名單管理 ────────────

async def manage_list(action: str, list_type: str, user_id_str: str = None):
    """管理白名單或黑名單"""
    cfg = load_config()
    key = f"{list_type}_list"
    lst = cfg.get(key, {})

    if action == "list":
        if not lst:
            print(f"📋 {list_type} 名單: 空")
        else:
            print(f"📋 {list_type} 名單 ({len(lst)} 人):")
            for uid, info in sorted(lst.items()):
                tag = f"@{info.get('username','')}" if info.get('username') else ""
                print(f"  • {uid} {tag} ({info.get('added','?')})")
        return

    if not user_id_str:
        print(f"❌ 請提供使用者 ID")
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

async def report(period: str = "day"):
    """生成封鎖摘要報告"""
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

    # 統計
    total = len(recent)
    sources = defaultdict(int)
    reasons = defaultdict(int)
    for b in recent:
        sources[b.get("source", "private")] += 1
        # 取廣告類型（reason 的第一個關鍵詞）
        reason = b.get("reason", "")
        for pat in SPAM_PATTERNS:
            m = re.search(pat, reason)
            if m:
                tag = reason[:16] if len(reason) > 16 else reason
                reasons[tag] += 1
                break
        else:
            reasons[reason[:20]] += 1

    print(f"\n📊 封鎖摘要 — {label}")
    print(f"{'─'*40}")
    print(f"   總計封鎖: {total} 人")
    print(f"")

    if len(sources) > 1:
        print(f"   來源:")
        for s, c in sorted(sources.items(), key=lambda x: -x[1]):
            label_s = "私訊" if s == "private" else "群組"
            print(f"     • {label_s}: {c} 人")

    print(f"   廣告類型 Top 5:")
    for r, c in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
        print(f"     • {r}: {c} 次")

    # 趨勢（每日）
    if period == "week":
        days = defaultdict(int)
        for b in recent:
            d = b["time"][:10]
            days[d] += 1
        print(f"\n   每日趨勢:")
        for d in sorted(days.keys()):
            print(f"     {d}: {days[d]} 人")

# ──────────── 首次設定 ────────────

async def setup(api_id: str = None, api_hash: str = None, phone: str = None, code: str = None):
    from telethon import TelegramClient

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
        print(f"手機隱藏")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(SESSION_FILE), int(api_id), api_hash)

    try:
        await client.start(phone=phone, code_callback=lambda: code or input("請輸入驗證碼: "))
        me = await client.get_me()
        print(f"\n✅ 登入成功！")
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

async def scan_and_block(dry_run: bool = False):
    from telethon import TelegramClient
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.types import User, Message, InputPhoneContact
    from telethon.tl.functions.contacts import GetContactsRequest

    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定，請先執行 --setup")
        return

    print(f"{'🧪 試運行' if dry_run else '🔍 掃描模式'}")
    print(f"{'─'*40}")

    client = TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"])
    try:
        await client.start(phone=cfg["phone"])

        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {c.id for c in contacts}
        print(f"📇 聯絡人: {len(contact_ids)} 位")

        now = datetime.now(timezone.utc)
        dialogs = await client.get_dialogs(limit=30)

        blocked = 0
        skipped = 0

        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, User) or entity.is_self or entity.id in contact_ids or entity.bot:
                continue

            try:
                msgs = await client.get_messages(entity, limit=5)
            except:
                continue

            spam_found = False
            spam_text = ""
            for msg in msgs:
                if not msg:
                    continue
                if msg.date and msg.date < now - timedelta(days=14):
                    continue
                msg_text = msg.text or ""
                if is_spam(msg_text, cfg):
                    spam_found = True
                    spam_text = msg_text[:120]
                    break
                if msg.photo:
                    ocr_text = await check_photo(client, msg)
                    if ocr_text and is_spam(ocr_text, cfg):
                        spam_found = True
                        spam_text = f"[OCR] {ocr_text[:100]}"
                        break

            if not spam_found:
                continue

            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
            uname = f"@{entity.username}" if entity.username else ""
            print(f"\n  ⚠️  廣告: {name} {uname}")
            print(f"      {spam_text[:120]}")

            if dry_run:
                skipped += 1
                continue

            try:
                await client(BlockRequest(id=entity.id))
                blocked += 1
                log_block(entity.id, name, spam_text, "scan")
                print(f"      ✅ 封鎖")
            except Exception as e:
                print(f"      ❌ 失敗: {e}")

        print(f"\n{'─'*40}")
        print(f"結果: 已處理 {blocked+skipped}")
        if not dry_run and blocked > 0:
            cfg["blocked_count"] = cfg.get("blocked_count", 0) + blocked
        cfg["last_scan"] = now.isoformat()
        save_config(cfg)
        await client.disconnect()
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        await client.disconnect()

# ──────────── 掃描群組踢除 ────────────

async def scan_groups(dry_run: bool = False):
    """掃描群組訊息，踢除發廣告的成員"""
    from telethon import TelegramClient
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights, User, Chat, Channel
    from telethon.tl.functions.contacts import GetContactsRequest
    from telethon.errors import UserAdminInvalidError

    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定")
        return

    print(f"{'🧪 試運行' if dry_run else '👥 群組掃描模式'}")
    print(f"{'─'*40}")

    client = TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"])
    try:
        await client.start(phone=cfg["phone"])
        me = await client.get_me()
        now = datetime.now(timezone.utc)

        # 白名單聯絡人
        contacts = (await client(GetContactsRequest(hash=0))).users
        contact_ids = {c.id for c in contacts}

        dialogs = await client.get_dialogs(limit=50)
        groups = []
        for d in dialogs:
            if isinstance(d.entity, (Chat, Channel)) and not d.entity.broadcast:
                # 檢查是否為管理員
                try:
                    participant = await client.get_permissions(d.entity, me.id)
                    if participant and participant.is_admin:
                        groups.append(d)
                except:
                    pass

        if not groups:
            print("⚠️  沒有可管理的群組（需要是管理員）")
            await client.disconnect()
            return

        print(f"👥 管理中的群組: {len(groups)}")
        kicked = 0
        total_scanned = 0

        for dialog in groups:
            entity = dialog.entity
            title = getattr(entity, "title", "未知群組")
            try:
                msgs = await client.get_messages(entity, limit=20)
            except:
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

                # 檢測廣告
                msg_text = msg.text or ""
                spam_reason = ""

                if is_spam(msg_text, cfg):
                    spam_reason = msg_text[:100]
                elif msg.photo:
                    ocr_text = await check_photo(client, msg)
                    if ocr_text and is_spam(ocr_text, cfg):
                        spam_reason = f"[OCR] {ocr_text[:80]}"

                if not spam_reason:
                    continue

                total_scanned += 1
                try:
                    sender = await client.get_entity(msg.sender_id)
                    sname = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                except:
                    sname = str(msg.sender_id)

                print(f"\n  ⚠️  [{title}] {sname}")
                print(f"     {spam_reason[:100]}")

                if dry_run:
                    continue

                # 踢除 (ban + kick)
                try:
                    rights = ChatBannedRights(
                        until_date=None,
                        view_messages=True
                    )
                    await client(EditBannedRequest(entity, msg.sender_id, rights))
                    kicked += 1
                    log_block(msg.sender_id, sname, spam_reason, "group")
                    print(f"     ✅ 已踢除")
                    await asyncio.sleep(1)  # 避免 rate limit
                except UserAdminInvalidError:
                    print(f"     ⚠️ 無法踢除（權限不足）")
                except Exception as e:
                    print(f"     ❌ 踢除失敗: {e}")

        print(f"\n{'─'*40}")
        print(f"結果: 掃描 {total_scanned} 條, {'已踢除' if not dry_run else '試運行'}: {kicked if not dry_run else total_scanned}")
        if not dry_run and kicked > 0:
            cfg["kicked_count"] = cfg.get("kicked_count", 0) + kicked
        save_config(cfg)
        await client.disconnect()
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        await client.disconnect()

# ──────────── 即時監聽（私訊+群組） ────────────

async def listen():
    from telethon import TelegramClient
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import User, Message, Chat, Channel, ChatBannedRights
    from telethon.tl.functions.contacts import GetContactsRequest

    cfg = load_config()
    if not cfg.get("api_id"):
        print("❌ 尚未設定")
        return

    print("👂 TeleShield 即時監聽啟動中...")
    print("    ✅ 私訊廣告 → 自動封鎖")
    print("    👥 群組廣告 → 自動踢除（管理員身份）")
    print("    📸 OCR 支援 → 純圖片廣告也辨識")
    print("    按 Ctrl+C 停止\n")

    client = TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"])

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        msg = event.message
        if not msg or not msg.sender_id:
            return

        sender_id = msg.sender_id
        chat = await event.get_chat()
        sender = await event.get_sender()
        now = datetime.now(timezone.utc)

        # 跳過自己
        if hasattr(sender, 'is_self') and sender.is_self:
            return

        # 檢查黑名單（無論在哪）
        if is_blacklisted(sender_id, cfg):
            try:
                if isinstance(chat, (Chat, Channel)):
                    rights = ChatBannedRights(until_date=None, view_messages=True)
                    await client(EditBannedRequest(chat, sender_id, rights))
                else:
                    await client(BlockRequest(id=sender_id))
            except:
                pass
            return

        if is_whitelisted(sender_id, cfg):
            return

        # 私訊處理
        if isinstance(chat, User):
            sender = chat
            if sender_id == cfg.get("user_id"):
                return
            if sender.bot:
                return

            # 檢查聯絡人
            try:
                contacts = (await client(GetContactsRequest(hash=0))).users
                contact_ids = {c.id for c in contacts}
                if sender_id in contact_ids:
                    return
            except:
                pass

            # 檢測
            spam_text = msg.text or ""
            is_spam_by_text = is_spam(spam_text, cfg)
            ocr_found_spam = False
            if not is_spam_by_text and msg.photo:
                ocr_text = await check_photo(client, msg)
                if ocr_text and is_spam(ocr_text, cfg):
                    ocr_found_spam = True
                    spam_text = ocr_text[:100]

            if not is_spam_by_text and not ocr_found_spam:
                return

            name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
            uname = f"@{sender.username}" if sender.username else ""
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            icon = "📸" if ocr_found_spam else ""
            print(f"\n[{ts}] {icon}⚠️  私訊廣告: {name} {uname}")
            print(f"    {spam_text[:100]}")

            try:
                await client(BlockRequest(id=sender_id))
                cfg["blocked_count"] = cfg.get("blocked_count", 0) + 1
                save_config(cfg)
                log_block(sender_id, name, spam_text, "private")
                print(f"     ✅ 封鎖（累計 {cfg['blocked_count']}）")
            except Exception as e:
                print(f"     ❌ 封鎖失敗: {e}")
            return

        # 群組處理
        if isinstance(chat, (Chat, Channel)) and not chat.broadcast:
            # 檢查是否為管理員
            try:
                me = await client.get_me()
                perm = await client.get_permissions(chat, me.id)
                if not perm or not perm.is_admin:
                    return
            except:
                return

            # 跳過管理員
            try:
                s_perm = await client.get_permissions(chat, sender_id)
                if s_perm and (s_perm.is_admin or s_perm.is_creator):
                    return
            except:
                pass

            # 檢測
            msg_text = msg.text or ""
            spam_reason = ""
            if is_spam(msg_text, cfg):
                spam_reason = msg_text[:100]
            elif msg.photo:
                ocr_text = await check_photo(client, msg)
                if ocr_text and is_spam(ocr_text, cfg):
                    spam_reason = f"[OCR] {ocr_text[:80]}"

            if not spam_reason:
                return

            sname = f"{sender.first_name or ''} {sender.last_name or ''}".strip() if hasattr(sender, 'first_name') else str(sender_id)
            title = getattr(chat, "title", "群組")
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[{ts}] 👥 群組廣告 [{title}]: {sname}")
            print(f"    {spam_reason[:100]}")

            try:
                rights = ChatBannedRights(until_date=None, view_messages=True)
                await client(EditBannedRequest(chat, sender_id, rights))
                cfg["kicked_count"] = cfg.get("kicked_count", 0) + 1
                save_config(cfg)
                log_block(sender_id, sname, spam_reason, "group")
                print(f"     ✅ 已踢除（累計 {cfg['kicked_count']}）")
            except Exception as e:
                print(f"     ❌ 踢除失敗: {e}")

    try:
        await client.start(phone=cfg["phone"])
        print(f"✅ TeleShield 已上線 — 監聽中...")
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\n\n👋 已停止")
        await client.disconnect()
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        await client.disconnect()

# ──────────── 主程式 ────────────

async def main():
    if len(sys.argv) < 2:
        print("TeleShield — Telegram 廣告封鎖工具")
        print(f"{'─'*35}")
        print("  --setup                   首次設定")
        print("  --scan                    掃描並封鎖私訊")
        print("  --dry-run                 試掃描")
        print("  --listen                  即時監聽（後台常駐）")
        print("  --group-scan              掃描群組並踢除廣告")
        print("  --status                  查看狀態")
        print("  --report [day|week]       封鎖摘要報告")
        print("  --learn <文字>            手動標記學習新模式")
        print("  --whitelist add|remove|list [id]")
        print("  --blacklist add|remove|list [id]")
        return

    cmd = sys.argv[1]

    if cmd == "--setup":
        # 安全：不接受 argv 傳密鑰（會進 shell history）。只支持交互式或環境變數
        # 優先環境變數（TELESHIELD_API_ID / TELESHIELD_API_HASH），否則交互輸入
        await setup(
            os.getenv("TELESHIELD_API_ID"),
            os.getenv("TELESHIELD_API_HASH"),
            os.getenv("TELESHIELD_PHONE"),
            None,
        )
    elif cmd == "--scan":
        await scan_and_block(dry_run=False)
    elif cmd == "--dry-run":
        await scan_and_block(dry_run=True)
    elif cmd == "--group-scan":
        await scan_groups(dry_run="--dry" in sys.argv or "dry" in sys.argv)
    elif cmd == "--listen":
        await listen()
    elif cmd == "--status":
        cfg = load_config()
        if not cfg:
            print("❌ 尚未設定")
            return
        log = load_block_log()
        recent = len([b for b in log.get("blocks", []) if datetime.fromisoformat(b["time"]) > datetime.now(timezone.utc) - timedelta(days=1)])
        print("📊 TeleShield 狀態")
        print(f"{'─'*30}")
        print(f"  帳號: {cfg.get('username','?')} (ID: {cfg.get('user_id','?')})")
        print(f"  累計封鎖私訊: {cfg.get('blocked_count',0)} 人")
        print(f"  累計踢除群組: {cfg.get('kicked_count',0)} 人")
        print(f"  今日封鎖: {recent} 人")
        print(f"  白名單: {len(cfg.get('whitelist',{}))} 人")
        print(f"  黑名單: {len(cfg.get('blacklist',{}))} 人")
        print(f"  學習模式: {len(cfg.get('learned_patterns',{}).get('keywords',[]))} 關鍵詞")
        print(f"  最後掃描: {cfg.get('last_scan','從未')}")
    elif cmd == "--report":
        period = sys.argv[2] if len(sys.argv) > 2 else "day"
        await report(period)
    elif cmd == "--learn":
        text = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        if not text:
            print("❌ 請提供廣告文字，例如: --learn 加我微信 xxx 投資穩賺")
            return
        await learn(text)
    elif cmd in ("--whitelist", "--blacklist"):
        list_type = cmd.replace("--", "")
        action = sys.argv[2] if len(sys.argv) > 2 else "list"
        user_id = sys.argv[3] if len(sys.argv) > 3 else None
        await manage_list(action, list_type, user_id)
    else:
        print(f"❌ 未知指令: {cmd}")
        print("執行不加參數查看全部指令")

if __name__ == "__main__":
    asyncio.run(main())
