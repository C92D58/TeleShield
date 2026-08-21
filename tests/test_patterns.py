"""SPAM_PATTERNS 正則測試 + 誤封回歸測試。"""

import pytest

from teleshield.patterns import SPAM_PATTERNS, extract_keywords, is_spam, learn_from_text


class TestSpamDetection:
    """廣告命中測試：每個內建模式至少有一個真實命中案例。"""

    @pytest.mark.parametrize(
        "text",
        [
            "加我微信 xiaoming888",
            "加V xiaoming888",
            "tg @telegroup2024",
            "快來 t.me/joinchat/xxx",
            "@promo_bot_2024",
            "兼職刷單 日入500",
            "投資理財穩賺 高回報",
            "裸聊約炮 包養",
            "賭博 六合彩 投注",
            "註冊送紅包 免費領",
            "刷粉漲粉 點贊關注",
            "出售批發 代購代發",
            "promote your channel",
            "click here",
            "earn money work from home",
            "free bitcoin airdrop",
            "limited offer 50% off",
        ],
    )
    def test_spam_hits(self, text):
        assert is_spam(text) is True, f"應命中廣告模式: {text!r}"


class TestFalsePositiveRegression:
    """誤封回歸：2026-08-21 收窄單字模式後的正常文本必須放行。"""

    @pytest.mark.parametrize(
        "text",
        [
            "你好，最近怎么样？",
            "这个周末我们出去玩吧",
            "我出钱请你吃饭",          # 「出」單字不再誤封
            "今天博客更新了",          # 「博」不再誤封
            "我刚看完一本小说",        # 「彩」相關誤封
            "谢谢你的关注",            # 關注但非刷粉
            "我们讨论一下投资的话题",  # 投資話題非廣告
            "她在博物馆工作",          # 博物館
            "我来参加你的生日派对",
            "收到，谢谢！",
        ],
    )
    def test_normal_text_not_spam(self, text):
        assert is_spam(text) is False, f"不應誤封正常文本: {text!r}"


class TestLearnFromText:
    def test_extract_keywords(self):
        kws = extract_keywords("加我微信 代购奶粉 稳赚")
        assert "代购奶粉" in kws or "稳赚" in kws

    def test_learn_extracts_wechat_handle(self):
        new_kws, new_patterns = learn_from_text("V: abc12345 稳赚", {"keywords": [], "patterns": []})
        assert any("abc12345" in p for p in new_patterns), f"應提取微信帳號模式: {new_patterns}"

    def test_learn_dedup(self):
        learned = {"keywords": ["稳赚"], "patterns": ["abc"]}
        new_kws, new_patterns = learn_from_text("稳赚 abc", learned)
        assert "稳赚" not in new_kws, "已有關鍵詞不應重複"
        assert "abc" not in new_patterns, "已有模式不應重複"

    def test_learn_empty(self):
        assert learn_from_text("", {}) == ([], [])

    def test_patterns_all_compile(self):
        import re

        for p in SPAM_PATTERNS:
            re.compile(p)  # 不拋異常即通過
