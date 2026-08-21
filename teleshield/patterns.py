"""廣告模式與判斷邏輯。

SPAM_PATTERNS 內建正則（收窄版：避免單字誤封）+ 學習模式（關鍵詞 + 自訂正則）。
純函數、零 I/O——可獨立單元測試。
"""

from __future__ import annotations

import re

__all__ = ["SPAM_PATTERNS", "is_spam", "learn_from_text", "extract_keywords"]

# 內建廣告模式（2026-08-21 收窄：去除「出/博/彩/售/賣」單字，改用組合詞）
SPAM_PATTERNS = [
    # 中文廣告常見模式
    r"加.{0,4}?微信|加[\s\-]*[LlvVxX]|vx[:：\s]|[LlvVxX][\s\-]*信",
    r"tg[\s\-]*@?[a-zA-Z0-9_]{3,}",
    r"(?:https?://)?t\.me/",
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

STOP_WORDS = {
    "我們", "他們", "可以", "沒有", "這個", "那個", "什麼", "因為", "所以", "但是",
    "如果", "雖然", "然後", "而且", "或者", "不過", "還是", "就是", "不是", "一個",
}


def is_spam(text: str, cfg: dict = None) -> bool:
    """檢查文字是否包含廣告模式（含自訂學習模式）。"""
    if not text:
        return False
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
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
