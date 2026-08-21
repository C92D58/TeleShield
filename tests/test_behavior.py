"""群組行為分析測試。"""

import time

from teleshield.behavior import BehaviorTracker


class TestBehaviorTracker:
    def test_recent_joiner_with_link_suspicious(self):
        t = BehaviorTracker()
        t.record_join(100, 42)
        t.record_message(100, 42, "大家好 https://t.me/spam")
        suspicious, reason = t.suspicious(100, 42)
        assert suspicious is True
        assert "join+link" in reason

    def test_link_burst_suspicious(self):
        t = BehaviorTracker()
        for i in range(3):
            t.record_message(100, 42, f"快来看 https://x.com/{i}")
        suspicious, reason = t.suspicious(100, 42)
        assert suspicious is True
        assert "link-burst" in reason

    def test_mention_burst_suspicious(self):
        t = BehaviorTracker()
        for i in range(3):
            t.record_message(100, 42, f"@user{i} @other{i} 加群")
        suspicious, reason = t.suspicious(100, 42)
        assert suspicious is True
        assert "mention-burst" in reason

    def test_normal_activity_not_suspicious(self):
        t = BehaviorTracker()
        t.record_message(100, 42, "今天天气不错")
        t.record_message(100, 42, "周末去哪玩")
        suspicious, _ = t.suspicious(100, 42)
        assert suspicious is False

    def test_joiner_without_link_not_suspicious(self):
        t = BehaviorTracker()
        t.record_join(100, 42)
        t.record_message(100, 42, "大家好，新人报到")
        suspicious, _ = t.suspicious(100, 42)
        assert suspicious is False

    def test_old_join_grace_expires(self):
        t = BehaviorTracker()
        t.record_join(100, 42)
        # 模擬時間流逝：直接改內部狀態
        t._joins[100][42] = time.time() - 600  # 10 分鐘前
        t.record_message(100, 42, "https://x.com/link")
        suspicious, _ = t.suspicious(100, 42)
        # 無爆發（只有 1 條連結）、寬限期已過 → 不可疑
        assert suspicious is False
