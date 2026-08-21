"""群組行為分析（v0.10.0）。

監控群組內的可疑行為模式：
- 新成員短時間內發連結 / 大量 @ → 疑似進群即廣告
- 短時間內連續發含連結消息（爆發）→ 疑似刷屏廣告

狀態存於記憶體（監聽進程內），不持久化。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque

__all__ = ["URL_RE", "MENTION_RE", "BehaviorTracker"]

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@\w{3,}")

# 新成員寬限期：進群 N 秒內發廣告視為高可疑
JOIN_GRACE_SECONDS = 300
# 爆發窗口：N 秒內 M 條含連結消息
BURST_WINDOW = 120
BURST_COUNT = 3


class BehaviorTracker:
    def __init__(self):
        # chat_id -> user_id -> deque[(timestamp, text)]
        self._messages: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=20)))
        # chat_id -> user_id -> 加入時間
        self._joins: dict[int, dict[int, float]] = defaultdict(dict)

    def record_join(self, chat_id: int, user_id: int) -> None:
        self._joins[chat_id][user_id] = time.time()

    def record_message(self, chat_id: int, user_id: int, text: str) -> None:
        self._messages[chat_id][user_id].append((time.time(), text))

    def is_recent_joiner(self, chat_id: int, user_id: int) -> bool:
        """用戶是否在寬限期內加入群組。"""
        ts = self._joins.get(chat_id, {}).get(user_id)
        return bool(ts) and (time.time() - ts) < JOIN_GRACE_SECONDS

    def link_burst_count(self, chat_id: int, user_id: int) -> int:
        """爆發窗口內含連結的消息數。"""
        now = time.time()
        msgs = self._messages.get(chat_id, {}).get(user_id, ())
        return sum(
            1 for ts, text in msgs
            if now - ts < BURST_WINDOW and URL_RE.search(text or "")
        )

    def mention_burst_count(self, chat_id: int, user_id: int) -> int:
        """爆發窗口內含大量 @ 提及的消息數。"""
        now = time.time()
        msgs = self._messages.get(chat_id, {}).get(user_id, ())
        return sum(
            1 for ts, text in msgs
            if now - ts < BURST_WINDOW and len(MENTION_RE.findall(text or "")) >= 2
        )

    def suspicious(self, chat_id: int, user_id: int) -> tuple[bool, str]:
        """評估群組行為可疑度，返回 (是否可疑, 原因)。

        - 新成員寬限期內發連結 → 可疑
        - 爆發窗口內 ≥3 條含連結消息 → 可疑
        - 爆發窗口內 ≥3 條大量 @ 消息 → 可疑
        """
        reasons = []
        if self.is_recent_joiner(chat_id, user_id):
            if self.link_burst_count(chat_id, user_id) >= 1:
                reasons.append(f"join+link({JOIN_GRACE_SECONDS}s)")
        if self.link_burst_count(chat_id, user_id) >= BURST_COUNT:
            reasons.append(f"link-burst>{BURST_WINDOW}s")
        if self.mention_burst_count(chat_id, user_id) >= BURST_COUNT:
            reasons.append(f"mention-burst>{BURST_WINDOW}s")
        return (bool(reasons), ";".join(reasons))
