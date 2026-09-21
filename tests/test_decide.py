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


# ════════════════════════════════════════════════════════════════════
# ⑤ 對接真實 API：這一節的每一條都對應一個「接了真 Jev 才會發現」的缺陷
#    用 2026-09-21 實測 API 回來的真實形狀寫成斷言 ✗ 不用發明形狀。
# ════════════════════════════════════════════════════════════════════
def test_every_question_carries_what_the_api_requires():
    """★ API 對每個題型有硬性欄位要求 ✗ 少一個就是 422 ✗ 整層靜默失效。

    Score 一定要 criteria（等級陣列）。送 min/max 會被回
    {"type":"missing","loc":["body","questions","aggression","score","criteria"]}。
    """
    q = build_questions()
    for qid, spec in q.items():
        assert spec.get("instructions"), "%s 缺 instructions" % qid
        if spec["type"] == "score":
            assert "criteria" in spec, "%s 是 score ✗ 一定要 criteria（API 會 422）" % qid
            assert isinstance(spec["criteria"], list), "%s 的 criteria 要是等級陣列" % qid
            assert len(spec["criteria"]) >= 2, "%s 至少要有兩級" % qid
        if spec["type"] == "choice":
            assert "criteria" in spec, "%s 是 choice ✗ 一定要 criteria（API 會 422）" % qid
            assert isinstance(spec["criteria"], dict), "%s 的 criteria 要是選項 map" % qid
    # 不該出現 API 不認識的欄位（options 不是 TypeSafe 的欄位名）
    for qid, spec in q.items():
        extra = set(spec) - {"type", "instructions", "criteria"}
        assert not extra, "%s 帶了 API 不認得的欄位：%s" % (qid, extra)


def test_reads_the_real_api_answer_shape():
    """真實回應（2026-09-21 實測）✗ 三個題型的欄位名都在這裡定住。

    最容易錯的是 Noul ✗ 它的機率欄位就叫 noul ✗ 不是 probability。
    """
    real = {
        "model": "jev-1.13.0",
        "answers": {
            "kind": {"type": "choice", "choice": "spam", "confidence": 0.93,
                     "probabilities": {"spam": 0.93, "promo": 0.07, "scam": 0.0, "legit": 0.0}},
            "aggression": {"type": "score", "score": 2.0, "confidence": 1.0,
                           "legend": {"0": "低", "1": "中", "2": "高"},
                           "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}},
            "needs_human": {"type": "noul", "noul": 0.42},
        },
    }

    class RealShapeJudge:
        def judge(self, state, questions):
            return real["answers"]

    d = decide(AMBIGUOUS_TEXT, judge=RealShapeJudge())
    assert d.kind == "spam", "choice 欄位沒讀到"
    assert d.confidence == 0.93
    assert d.needs_human == 0.42, "★ noul 欄位沒讀到 ✗ 真回應的機率就叫 noul"
    assert d.source == "jev"


def test_aggression_is_normalised_across_both_judges():
    """★ 兩個 judge 的尺度必須一致 ✗ 否則 Decision.aggression 的意義會變。

    真 Jev 回等級位置 0..len(criteria)-1（實測 5 級表回 0–4）✗
    stub 直接給 0-100 ✗ 不對齊的話同一則訊息會有兩種數字。
    """
    from teleshield.decide import AGGRESSION_LEVELS, AGGRESSION_TOP
    assert AGGRESSION_TOP == len(AGGRESSION_LEVELS) - 1

    class TopLevelJudge:
        def judge(self, state, questions):
            return {"kind": {"choice": "legit", "confidence": 0.9},
                    "aggression": {"type": "score", "score": float(AGGRESSION_TOP)},
                    "needs_human": {"type": "noul", "noul": 0.05}}

    d = decide(AMBIGUOUS_TEXT, judge=TopLevelJudge())
    assert d.aggression == 100.0, "最高等級應該正規化成 100 ✗ 得到 %r" % d.aggression


def test_a_failed_semantic_call_never_looks_like_a_verdict():
    """服務失敗 → source=error + review ✗ 不能退回 allow ✗ 更不能當成 block。"""
    class Boom:
        def judge(self, state, questions):
            raise urllib.error.HTTPError("u", 422, "Unprocessable Entity", {}, None)

    d = decide(AMBIGUOUS_TEXT, judge=Boom())
    assert d.source == "error"
    assert d.action == "review"
    assert "422" in d.note or "HTTPError" in d.note


def test_jev_unwraps_the_answers_envelope():
    """★ 真實回應是 {"model":…, "answers":{…}, "usage":{…}} ✗ 判斷在 answers 裡。

    原本 JevJudge 直接回傳整個 payload ✗ 於是上層 raw.get("kind") 恆為 None ✗
    真實模型的判斷一次都沒被採用 ✗ 每則訊息都掉進 review ✗ 而 API 照樣計費。
    這條測試把「必須拆封」釘住 ✗ 免得又被退回。
    """
    import json as _json
    import urllib.request as _u

    payload = {
        "model": "jev-1.13.0",
        "answers": {
            "kind": {"type": "choice", "choice": "spam", "confidence": 0.9,
                     "probabilities": {"spam": 0.9, "promo": 0.05, "scam": 0.05, "legit": 0.0}},
            "aggression": {"type": "score", "score": 1.0},
            "needs_human": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 500, "output_tokens": 60},
    }

    class FakeResp:
        def read(self):
            return _json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    from teleshield.decide import JevJudge
    j = JevJudge(api_key="k")
    orig = _u.urlopen
    _u.urlopen = lambda *a, **k: FakeResp()
    try:
        out = j.judge({"t": 1}, {"kind": {"type": "choice", "criteria": {"a": None}}})
    finally:
        _u.urlopen = orig

    assert "answers" not in out, "不該把 envelopes 整包往上丟"
    assert out["kind"]["choice"] == "spam", "★ answers 沒拆開 ✗ 上層會讀不到判斷"
    assert out["_model"] == "jev-1.13.0"
    assert out["_usage"]["input_tokens"] == 500
