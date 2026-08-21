"""廣告模式與判斷邏輯。

分級模式（v0.10.0）：
- SEVERE：直接封鎖級（賭博/色情/刷單/裸聊）
- MODERATE：明顯廣告（投資/兼職/出售/加微信）
- LOW：弱信號（t.me 連結/@ 提及/優惠碼），單獨命中不足以封鎖

SPAM_PATTERNS 為三級合併的平列表（向後相容 is_spam()）。
純函數、零 I/O——可獨立單元測試。
"""

from __future__ import annotations

import re

__all__ = [
    "SEVERE_PATTERNS",
    "MODERATE_PATTERNS",
    "LOW_PATTERNS",
    "SPAM_PATTERNS",
    "is_spam",
    "match_patterns",
    "learn_from_text",
    "extract_keywords",
]

# ─── 高危：直接封鎖級（繁簡並收，語義簇獨立計分）───
SEVERE_PATTERNS = [
    # 加微信/加V/V信：陌生非聯絡人說此 = 引流廣告（聯絡人/白名單已在掃描層排除）
    r"加.{0,4}?微信|加[\s\-]*[LlvVxX]|vx[:：\s]|[LlvVxX][\s\-]*信",
    # 色情類
    r"色情|A片|成人|裸聊",
    r"約炮|约炮|援交|包養|包养",
    # 賭博類
    r"賭博|赌博|博彩|賭場|赌场|賭球",
    r"六合彩|開獎|开奖|下注|投注|casino|betting",
    # 兼職/刷單類
    r"兼職|兼职|刷單|刷单",
    r"日入|月入|日赚|躺賺|躺赚|被動收入|在家工作|在家赚钱|輕鬆賺|轻松赚",
    # 金融詐騙類
    r"裸貸|裸贷|借貸|套現|套现|跑分|洗錢",
]

# ─── 中危：明顯廣告（繁簡並收，需組合或疊加）───
MODERATE_PATTERNS = [
    # 投資理財類
    r"投資|投资|理財|理财",
    r"帶單|带单|跟單|跟单|量化",
    r"穩賺|稳赚|穩健|高回報|高收益|高回报",
    # 交易出售類
    r"出售|售卖|批發|批发|代購|代购",
    r"代發|代发|供應商|出貨|庫存甩賣|清倉|清仓",
    # 優惠引流類
    r"註冊送|注册送",
    r"免費領|免费领",
    r"紅包|禮金|優惠碼|优惠码|推廣碼|推广码",
    # 刷量類
    r"點贊|点赞|刷粉|刷讚|刷赞|漲粉|涨粉",
    r"關注|关注",
    # 英文推廣
    r"promote|promotion|advertisement|sponsor",
    r"earn\s*money|passive\s*income",
    r"work\s*from\s*home",
    r"free\s*crypto|free\s*bitcoin",
    r"airdrop|giveaway",
    r"limited\s*offer|buy\s*now",
    r"discount\s*\d{2,}%|\d{2,}%\s*off",
]

# ─── 低危：弱信號，需組合 ───
LOW_PATTERNS = [
    r"tg[\s\-]*@?[a-zA-Z0-9_]{3,}",
    r"(?:https?://)?t\.me/",
    r"@\w{4,}",  # 單獨 @ 提及暫不封鎖
    r"click\s*(here|this\s*link|the\s*link)",
]

# 合併平列表（向後相容）
SPAM_PATTERNS = SEVERE_PATTERNS + MODERATE_PATTERNS + LOW_PATTERNS


def match_patterns(text: str) -> tuple[str, list[str]]:
    """返回 (最高嚴重級, 命中的模式列表)。未命中返回 (None, [])。"""
    if not text:
        return None, []
    hits = []
    max_tier = None
    for tier, patterns in (
        ("severe", SEVERE_PATTERNS),
        ("moderate", MODERATE_PATTERNS),
        ("low", LOW_PATTERNS),
    ):
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                hits.append(p)
                if max_tier is None or _tier_weight(tier) > _tier_weight(max_tier):
                    max_tier = tier
    return max_tier, hits


def _tier_weight(tier) -> int:
    return {"severe": 3, "moderate": 2, "low": 1}.get(tier, 0)


def _hits_score(hits: list[str]) -> int:
    """按命中模式疊加計分（同級多次命中加分，上限 9）。"""
    score = 0
    for h in hits[:10]:
        if h in SEVERE_PATTERNS:
            score += 3
        elif h in MODERATE_PATTERNS:
            score += 2
        else:
            score += 1
    return min(score, 9)


def is_spam(text: str, cfg: dict = None) -> bool:
    """檢查文字是否為明顯廣告。

    語義（v0.10.0）：疊加計分 ≥3 或命中學習模式才判定。
    單一弱信號（t.me 連結/@ 提及/單個投資詞）不再誤判。
    """
    if not text:
        return False
    _, hits = match_patterns(text)
    if _hits_score(hits) >= 3:
        return True
    if cfg:
        lp = cfg.get("learned_patterns", {})
        for p in lp.get("patterns", []):
            try:
                if re.search(p, text, re.IGNORECASE):
                    return True
            except re.error:
                continue
        for kw in lp.get("keywords", []):
            if kw.lower() in text.lower():
                return True
    return False


STOP_WORDS = {
    "我們", "他們", "可以", "沒有", "這個", "那個", "什麼", "因為", "所以", "但是",
    "如果", "雖然", "然後", "而且", "或者", "不過", "還是", "就是", "不是", "一個",
}


def extract_keywords(text: str) -> list[str]:
    """提取 2-6 字中文關鍵詞，過濾常見停用詞。"""
    tokens = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    return [t for t in tokens if t not in STOP_WORDS]


def learn_from_text(text: str, learned: dict) -> tuple[list[str], list[str]]:
    """從廣告文字提取新關鍵詞與新模式，返回 (new_keywords, new_patterns)。

    純函數：不寫入任何存儲，由調用方決定持久化。
    """
    if not text:
        return [], []

    keywords = set(learned.get("keywords", []))
    patterns = set(learned.get("patterns", []))

    new_kws = [t for t in extract_keywords(text) if t not in keywords]
    keywords.update(new_kws)

    new_patterns = []
    # 微信/Line/WhatsApp 類：加/V/薇/威 + 帳號
    m = re.search(r'(加|V|v|薇|威|wechat|line|whatsapp)[-:\s]*([a-zA-Z0-9_]{4,})', text)
    if m:
        pat = re.escape(m.group(2))
        if pat not in patterns:
            new_patterns.append(pat)
            patterns.add(pat)

    # URL 短網址
    urls = re.findall(r'https?://[^\s]{4,}', text)
    for u in urls:
        pat = re.escape(u[:20])
        if pat not in patterns:
            new_patterns.append(pat)
            patterns.add(pat)

    # 沒有可提取內容時，保存整句片段
    if not new_kws and not new_patterns:
        phrase = re.escape(text[:30])
        new_patterns.append(phrase)

    return new_kws, new_patterns
