"""垃圾訊號評分引擎（v0.10.0）。

多維評分取代單層正則判定：
- 正則命中（按嚴重級加權：severe +3 / moderate +2 / low +1）
- 連結密度：超過 2 個 URL 加分
- 大量 @ 提及：加分
- 帳號特徵：無 username、無頭像、無 bio（新垃圾帳號特徵）
- 頻率：同用戶短時間多條消息（監聽模式由調用方傳入）

決策閾值：
- score >= 5  → block（直接封鎖/踢除）
- score >= 3  → flag（僅記錄標記，不採取動作）
- score <  3  → pass
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .patterns import MODERATE_PATTERNS, SEVERE_PATTERNS, _tier_weight, match_patterns

__all__ = [
    "BLOCK_THRESHOLD",
    "FLAG_THRESHOLD",
    "Verdict",
    "ScoringResult",
    "SpamScorer",
]

BLOCK_THRESHOLD = 5
FLAG_THRESHOLD = 3

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@\w{3,}")


class Verdict:
    PASS = "pass"
    FLAG = "flag"
    BLOCK = "block"


@dataclass
class ScoringResult:
    score: int = 0
    verdict: str = Verdict.PASS
    reasons: list[str] = field(default_factory=list)
    tier: str | None = None
    pattern_hits: list[str] = field(default_factory=list)


class SpamScorer:
    """評分器。可選 user_info 提供帳號特徵、recent_activity 提供頻率信息。"""

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or {}

    def score(
        self,
        text: str = "",
        user_info: dict = None,
        recent_messages: int = 0,
        learned: dict = None,
    ) -> ScoringResult:
        result = ScoringResult()
        if not text and not user_info:
            return result

        # 1. 正則命中（分級加權，同級疊加）
        tier, hits = match_patterns(text)
        if hits:
            for h in hits[:5]:
                result.score += _tier_weight(
                    "severe" if h in SEVERE_PATTERNS
                    else "moderate" if h in MODERATE_PATTERNS
                    else "low"
                )
            result.tier = tier
            result.pattern_hits = hits[:5]
            result.reasons.append(f"pattern:{tier}x{len(hits)}")

        # 2. 自訂學習模式（關鍵詞/自訂正則——視為 moderate）
        lp = learned or self.cfg.get("learned_patterns", {})
        if lp:
            for p in lp.get("patterns", []):
                try:
                    if re.search(p, text, re.IGNORECASE):
                        result.score += 2
                        result.reasons.append("learned:pattern")
                        break
                except re.error:
                    continue
            for kw in lp.get("keywords", []):
                if kw and kw.lower() in text.lower():
                    result.score += 2
                    result.reasons.append(f"learned:kw:{kw[:10]}")
                    break

        # 3. 連結密度
        urls = URL_RE.findall(text)
        if len(urls) >= 3:
            result.score += 1
            result.reasons.append("links:3+")
        elif len(urls) >= 1 and result.score >= FLAG_THRESHOLD:
            # 已有信號 + 連結 = 更強
            result.score += 1
            result.reasons.append("link+signal")

        # 4. 大量 @ 提及
        mentions = MENTION_RE.findall(text)
        if len(mentions) >= 2:
            result.score += 1
            result.reasons.append("mentions:2+")

        # 5. 帳號特徵（僅私訊非聯絡人場景有意義）
        if user_info:
            weak = 0
            if not user_info.get("username"):
                weak += 1
            if not user_info.get("photo"):
                weak += 1
            if not user_info.get("bio"):
                weak += 1
            if weak >= 2 and result.score >= 1:
                result.score += 1
                result.reasons.append(f"weak_profile:{weak}/3")

        # 6. 頻率：短時間多條消息
        if recent_messages >= 5:
            result.score += 1
            result.reasons.append("burst:5+")

        # 決策
        if result.score >= BLOCK_THRESHOLD:
            result.verdict = Verdict.BLOCK
        elif result.score >= FLAG_THRESHOLD:
            result.verdict = Verdict.FLAG
        return result
