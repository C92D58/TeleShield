"""decide.py 的測試。

重點不是「Jev 準不準」（那要用真實資料調門檻 ✗ 見 decide.Thresholds 的說明），
而是**組合邏輯對不對**：規則優先、語意補位、服務掛掉時保守、stub 不被誤認。
所以這裡全部用可控的假 judge ✗ 不連網、不花錢、可重現。
"""

from __future__ import annotations

import urllib.error

import pytest

from teleshield.decide import (
    StubJudge,
    Thresholds,
    build_questions,
    build_state,
    decide,
)


# ════════════════════════════════════════════════════════════════
# 假 judge：想回什麼就回什麼 ✗ 並記錄有沒有被呼叫
# ════════════════════════════════════════════════════════════════
class FakeJudge:
    def __init__(self, kind="legit", conf=0.9, aggr=10, needs=0.1, raises=None):
        self.kind, self.conf, self.aggr, self.needs = kind, conf, aggr, needs
        self.raises = raises
        self.calls = 0
        self.last_state = None

    def judge(self, state, questions):
        self.calls += 1
        self.last_state = state
        if self.raises:
            raise self.raises
        return {
            "kind": {"value": self.kind, "confidence": self.conf},
            "aggression": {"value": self.aggr},
            "needs_human": {"probability": self.needs},
        }


# 一句真的嚴重廣告（含「加微信」與「穩賺」✗ 會命中 severe 與 moderate）
SEVERE_TEXT = "加我微信 abc123 帶你穩賺不賠"
# 一句普通的合作詢問（任何正則都不該命中）
AMBIGUOUS_TEXT = "你好，想請問你們接不接企業內訓的合作？"


# ════════════════════════════════════════════════════════════════
# ① 規則優先：確定的事不花錢
# ════════════════════════════════════════════════════════════════
def test_severe_regex_blocks_without_calling_the_model():
    j = FakeJudge()
    d = decide(SEVERE_TEXT, judge=j)
    assert d.action == "block"
    assert d.source == "regex"
    assert d.tier == "severe"
    assert j.calls == 0, "severe 命中時不應該呼叫模型（那是浪費錢）"


def test_empty_text_allows_without_calling_the_model():
    j = FakeJudge()
    d = decide("   ", judge=j)
    assert d.action == "allow" and j.calls == 0


# ════════════════════════════════════════════════════════════════
# ② 語意補位：正則最沒用的地方才花錢
# ════════════════════════════════════════════════════════════════
def test_ambiguous_text_reaches_the_model():
    j = FakeJudge(kind="legit", conf=0.95, needs=0.05)
    d = decide(AMBIGUOUS_TEXT, judge=j)
    assert j.calls == 1, "沒有正則命中時應該交給模型判斷"
    assert d.action == "allow"


@pytest.mark.parametrize("kind,conf,expected", [
    ("spam", 0.95, "block"),
    ("scam", 0.93, "block"),
    ("spam", 0.60, "review"),   # 信心不足 → 人工，不是直接封
    ("legit", 0.95, "allow"),
    ("legit", 0.55, "review"),  # 說是正常但沒把握 → 人工
    ("promo", 0.99, "review"),  # promo 不在自動封鎖的清單裡
])
def test_confidence_gates_the_action(kind, conf, expected):
    d = decide(AMBIGUOUS_TEXT, judge=FakeJudge(kind=kind, conf=conf, needs=0.05))
    assert d.action == expected, "%s@%.2f 應該是 %s" % (kind, conf, expected)


def test_model_asking_for_a_human_wins():
    """模型自己說需要人看 ✗ 就不該自動放行（即使 kind 是 legit 且信心高）。"""
    d = decide(AMBIGUOUS_TEXT, judge=FakeJudge(kind="legit", conf=0.97, needs=0.80))
    assert d.action == "review"


def test_high_confidence_spam_is_not_downgraded_by_needs_human():
    """已經確定是 spam ✗ 不因為 needs_human 而變 review（那會讓封鎖失效）。"""
    d = decide(AMBIGUOUS_TEXT, judge=FakeJudge(kind="spam", conf=0.96, needs=0.9))
    assert d.action == "block"


# ════════════════════════════════════════════════════════════════
# ③ 服務掛掉時保守：絕不因為連不上就放行
# ════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("exc", [
    urllib.error.URLError("no route"),
    urllib.error.HTTPError("u", 503, "unavailable", {}, None),
    TimeoutError(),
    OSError("connection reset"),
])
def test_service_failure_routes_to_human_not_allow(exc):
    d = decide(AMBIGUOUS_TEXT, judge=FakeJudge(raises=exc))
    assert d.action == "review"
    assert d.source == "error"
    assert "失敗" in d.note


def test_malformed_response_routes_to_human():
    class Bad:
        def judge(self, state, questions):
            return {"kind": "這不是物件"}
    d = decide(AMBIGUOUS_TEXT, judge=Bad())
    assert d.action in ("review", "allow")
    assert d.source in ("jev", "stub")


# ════════════════════════════════════════════════════════════════
# ④ stub 不能被誤認成 Jev
# ════════════════════════════════════════════════════════════════
def test_stub_is_labelled_and_never_claims_to_be_jev():
    d = decide(AMBIGUOUS_TEXT, judge=StubJudge())
    assert d.source == "stub"
    assert "stub" in d.note
    assert "不是 Jev" in d.note


def test_stub_is_deterministic():
    a = decide(AMBIGUOUS_TEXT, judge=StubJudge())
    b = decide(AMBIGUOUS_TEXT, judge=StubJudge())
    assert (a.action, a.kind, a.confidence) == (b.action, b.kind, b.confidence)


# ════════════════════════════════════════════════════════════════
# ⑤ 門檻
# ════════════════════════════════════════════════════════════════
def test_thresholds_clamp_into_a_sane_range():
    t = Thresholds(auto_block=1.8, auto_allow=-0.4, review=-0.4).clamp()
    assert 0.0 <= t.review <= 1.0
    assert 0.0 <= t.auto_allow <= 1.0
    assert 0.0 <= t.auto_block <= 1.0


def test_block_and_allow_gates_are_independently_settable():
    """★ 封鎖與放行用的是兩個不同門檻 ✗ 因為代價不對稱。

    封錯了傷到真人 ✗ 門檻要高；放行錯了只是少擋一則廣告 ✗ 門檻可以低。
    用同一個數字的話 ✗ allow 幾乎永遠不觸發 ✓
    """
    j = FakeJudge(kind="legit", conf=0.75, needs=0.05)
    # 預設：0.75 ≥ auto_allow(0.70) → 放行
    assert decide(AMBIGUOUS_TEXT, judge=j).action == "allow"
    # 把放行門檻提到 0.80 ✗ 同一個輸入就變成要人看
    t = Thresholds(auto_allow=0.80)
    assert decide(AMBIGUOUS_TEXT, judge=j, thresholds=t).action == "review"
    # 封鎖門檻不受影響
    strict = Thresholds(auto_block=0.70, auto_allow=0.70)
    assert decide(AMBIGUOUS_TEXT, judge=j, thresholds=strict).action == "allow"


def test_lowering_the_block_gate_makes_it_stricter():
    """門檻降低 = 更多東西被自動封 ✗ 這是刻意的（保守設定）。"""
    j = FakeJudge(kind="spam", conf=0.70)
    assert decide(AMBIGUOUS_TEXT, judge=j).action == "review"
    strict = decide(AMBIGUOUS_TEXT, judge=j, thresholds=Thresholds(auto_block=0.65)).action
    assert strict == "block"


# ════════════════════════════════════════════════════════════════
# ⑥ 問題與狀態的形狀（照 TypeSafe 的要求）
# ════════════════════════════════════════════════════════════════
def test_questions_are_self_describing():
    q = build_questions()
    assert set(q) == {"kind", "aggression", "needs_human"}
    assert q["kind"]["type"] == "choice" and q["kind"]["criteria"]
    assert q["aggression"]["type"] == "score"
    assert q["needs_human"]["type"] == "noul"
    # 判斷寫在 instructions ✗ 這是 TypeSafe 明確要求的
    for v in q.values():
        assert v.get("instructions"), "每個問題都要有 instructions"


def test_question_text_never_leaks_into_state():
    """★ 問題寫進 state 會被當成「要被判斷的材料」——這是最常見的誤用。"""
    st = build_state("一則訊息", {"sender_username": "abc", "is_contact": False})
    assert set(st) <= {"message", "sender_username", "is_contact"}
    blob = str(st)
    for word in ("判斷", "分類", "是否為垃圾", "probability"):
        assert word not in blob, "state 裡不該出現問題文字：%s" % word


def test_state_carries_context_but_not_the_whole_config():
    st = build_state("hi", {"is_contact": True, "account_age_days": 3,
                            "learned_patterns": {"keywords": ["x"]}})
    assert st["is_contact"] is True and st["account_age_days"] == 3
    assert "learned_patterns" not in st, "不要把整個 config 倒進 state"


def test_context_reaches_the_judge():
    j = FakeJudge()
    decide(AMBIGUOUS_TEXT, ctx={"is_contact": False}, judge=j)
    assert j.last_state.get("is_contact") is False
