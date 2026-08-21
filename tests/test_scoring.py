"""評分引擎測試：分級、閾值、決策。"""


from teleshield.scoring import (
    FLAG_THRESHOLD,
    SpamScorer,
    Verdict,
)


class TestScoringTiers:
    """分級加權：severe +3 / moderate +2 / low +1。"""

    def test_severe_alone_blocks(self):
        # 賭博（severe +3）+ 連結（+1）= 4 → flag；加帳號弱特徵 = 5 → block
        res = SpamScorer().score("快来赌博 稳赢 https://evil.com")
        assert res.tier == "severe"
        assert res.verdict in (Verdict.FLAG, Verdict.BLOCK)
        assert res.score >= FLAG_THRESHOLD

    def test_moderate_alone_flags(self):
        # 投資理財多簇疊加（穩賺+高回报= 2 簇 ×2）= 4 → flag
        res = SpamScorer().score("高回报投资 稳赚")
        assert res.verdict == Verdict.FLAG

    def test_low_alone_passes(self):
        # 單一 t.me 連結（low +1）→ pass
        res = SpamScorer().score("看我这个 https://t.me/joinchat/xxx")
        assert res.verdict == Verdict.PASS

    def test_single_moderate_word_passes(self):
        # 單一投資詞（+2）→ pass（正常討論投資話題不誤判）
        res = SpamScorer().score("我们讨论一下投资的话题")
        assert res.verdict == Verdict.PASS

    def test_low_plus_learned_blocks(self):
        # t.me（+1）+ 學習關鍵詞（+2）+ 穩賺（+2）+ 連結獎勵 = 6 → block
        res = SpamScorer({"learned_patterns": {"keywords": ["稳赚"], "patterns": []}}).score(
            "稳赚 https://t.me/joinchat/abc"
        )
        assert res.verdict == Verdict.BLOCK

    def test_normal_text_passes(self):
        res = SpamScorer().score("你好，今天天气不错，周末一起吃饭吗？")
        assert res.verdict == Verdict.PASS
        assert res.score == 0


class TestScoringThresholds:
    def test_multi_link_bonus(self):
        # 3+ 連結加分
        res = SpamScorer().score("https://a.com/x https://b.com/y https://c.com/z")
        assert "links:3+" in res.reasons

    def test_mentions_bonus(self):
        res = SpamScorer().score("@user1 @user2 @user3 加群")
        assert "mentions:2+" in res.reasons

    def test_weak_profile_bonus(self):
        res = SpamScorer().score("稳赚 高回报", user_info={"username": None, "photo": None, "bio": None})
        assert any(r.startswith("weak_profile") for r in res.reasons)

    def test_burst_bonus(self):
        res = SpamScorer().score("刷单 兼职", recent_messages=6)
        assert "burst:5+" in res.reasons

    def test_learned_patterns_counted(self):
        res = SpamScorer().score(
            "加我微信 abc123",
            learned={"keywords": [], "patterns": [r"abc123"]},
        )
        assert res.score >= 2


class TestVerdictConsistency:
    def test_block_requires_threshold(self):
        s = SpamScorer()
        assert s.score("赌博 赌场 六合彩 下注").verdict == Verdict.BLOCK

    def test_pass_never_blocked(self):
        s = SpamScorer()
        for text in ["普通消息", "好的收到", "今天加班", "谢谢啦"]:
            assert s.score(text).verdict == Verdict.PASS
