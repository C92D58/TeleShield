"""本機 ML 層接進 decide() 之後的行為。

★ 為什麼這個檔案存在：ml.py 有自己的 48 個測試 ✗ 但那測的是分類器本身 ✗
把分類器接進決策管線的這幾十行沒有被任何人測到。而它動的是**封鎖判斷**——
出錯的代價是誤封真人。所以方向性必須被測試釘死：

    這一層只被允許把 review 升級成 block
    它永遠不會把 block 降級 ✗ 也不會把 allow 升級

沒有模型時整條管線必須與接線前**完全一樣**——這是回歸保護。
"""

from __future__ import annotations

import pytest

from teleshield import decide as D


class _FakeModel:
    """只實作 _ml_spam_score 會用到的介面。"""

    def __init__(self, p_spam: float):
        self.p_spam = p_spam
        self.classes = ["legit", "spam"]
        self.n_samples = 10
        self.vocab_size = 3

    def predict_proba(self, text):  # noqa: ARG002
        return {"spam": self.p_spam, "legit": 1.0 - self.p_spam}


def _judge(kind, probs=None, needs=0.0, aggression=1.0):
    """造一個回傳固定答案的 judge（形狀照真 Jev 的回應）。"""

    class J:
        def judge(self, state, questions):  # noqa: ARG002
            return {
                "kind": {"value": kind, "confidence": max(probs.values()) if probs else 0.5,
                         "probabilities": probs or {}},
                "aggression": {"value": aggression},
                "needs_human": {"noul": needs},
            }

    return J()


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(D, "_ml_spam_score", lambda text: None)


@pytest.fixture
def model_factory(monkeypatch):
    def install(p_spam):
        monkeypatch.setattr(D, "_ml_spam_score", lambda text: p_spam)

    return install


# ════════════════════════════════════════════════════════════════════
# 沒有模型 = 完全回歸
# ════════════════════════════════════════════════════════════════════
def test_without_model_nothing_changes(no_model):
    """沒有模型時 ✗ ml_score 是 None ✗ 行為與接線前一致。"""
    d = D.decide("你好，想問你們接不接企業內訓", judge=_judge("legit", {"legit": 0.9}))
    assert d.ml_score is None
    assert d.action == "allow"


def test_without_model_review_stays_review(no_model):
    """模糊訊息沒有模型時仍然是 review ✗ 不會被莫名升級。"""
    d = D.decide("在嗎", judge=_judge("promo", {"promo": 0.5, "spam": 0.3}, needs=0.9))
    assert d.ml_score is None
    assert d.action == "review"


def test_ml_score_is_recorded_even_when_not_blocking(model_factory):
    """分數要留下來 ✗ 不然事後無法檢討這層的表現。"""
    model_factory(0.55)
    d = D.decide("在嗎", judge=_judge("promo", {"promo": 0.5, "spam": 0.3}, needs=0.9))
    assert d.ml_score == 0.55
    assert d.action == "review"


# ════════════════════════════════════════════════════════════════════
# 升級：review → block
# ════════════════════════════════════════════════════════════════════
def test_confident_model_upgrades_review_to_block(model_factory):
    model_factory(0.97)
    d = D.decide("在嗎", judge=_judge("promo", {"promo": 0.5, "spam": 0.3}, needs=0.9))
    assert d.action == "block"
    assert "本機模型" in d.note
    assert d.ml_score == 0.97


def test_below_threshold_does_not_upgrade(model_factory):
    """門檻是 0.90 ✗ 0.89 不該封。"""
    model_factory(0.89)
    d = D.decide("在嗎", judge=_judge("promo", {"promo": 0.5, "spam": 0.3}, needs=0.9))
    assert d.action == "review"


def test_threshold_is_configurable(model_factory):
    model_factory(0.60)
    th = D.Thresholds(ml_block=0.50)
    d = D.decide("在嗎", judge=_judge("promo", {"promo": 0.5, "spam": 0.3}, needs=0.9),
                 thresholds=th)
    assert d.action == "block"


# ════════════════════════════════════════════════════════════════════
# 方向性：只往保守推
# ════════════════════════════════════════════════════════════════════
def test_never_downgrades_a_block(no_model, model_factory):
    """已判定封鎖 ✗ 模型說很低 ✗ 也不能放掉。"""
    model_factory(0.01)
    d = D.decide("加我微信穩賺", judge=_judge("spam", {"spam": 0.95}))
    assert d.action == "block"


def test_never_upgrades_an_explicit_allow(model_factory):
    """★ 最重要的一條：模型說 0.99 也不能把「已放行」翻成封鎖。

    放行錯誤只是少擋一則廣告 ✗ 誤封真人代價高得多 ✗
    所以弱模型不准往「更激進」的方向推。
    """
    model_factory(0.99)
    d = D.decide("你好，想問你們接不接企業內訓", judge=_judge("legit", {"legit": 0.95}))
    assert d.action == "allow"
    assert d.ml_score == 0.99  # 分數有記下來 ✗ 只是沒有被用來放行


# ════════════════════════════════════════════════════════════════════
# 語意層掛掉時：本機模型是唯一還活著的判斷
# ════════════════════════════════════════════════════════════════════
def test_api_failure_with_confident_model_blocks(model_factory):
    model_factory(0.95)

    class Boom:
        def judge(self, state, questions):
            raise OSError("network down")

    # ★ 文字要選**不會命中 severe 正則**的 ✗ 否則 decide() 在第一層就回傳了 ✗
    #   永遠走不到語意層 ✗ 這個測試就會變成在測正則（我第一版就是這樣寫錯的）
    d = D.decide("在嗎，想跟你聊聊合作", judge=Boom())
    assert d.source == "error"
    assert d.action == "block"
    assert "本機模型" in d.note


def test_api_failure_without_model_stays_review(no_model):
    class Boom:
        def judge(self, state, questions):
            raise OSError("network down")

    d = D.decide("在嗎", judge=Boom())
    assert d.action == "review"
    assert d.source == "error"


def test_bad_response_shape_with_confident_model_blocks(model_factory):
    model_factory(0.93)

    class Weird:
        def judge(self, state, questions):
            return {"kind": {"value": 12345}}  # 形狀不對

    d = D.decide("在嗎", judge=Weird())
    assert d.ml_score == 0.93
    assert isinstance(d.action, str)


# ════════════════════════════════════════════════════════════════════
# 載入與快取
# ════════════════════════════════════════════════════════════════════
def test_get_ml_model_returns_none_when_no_file(monkeypatch, tmp_path):
    from teleshield import ml
    monkeypatch.setattr(ml, "DEFAULT_MODEL_FILE", tmp_path / "nope.json")
    D._ML_CACHE.update({"mtime": None, "model": None})
    assert D.get_ml_model() is None
    assert D._ml_spam_score("x") is None


def test_model_cache_reloads_when_file_changes(monkeypatch, tmp_path):
    """改了就該生效 ✗ 不能永遠快取。"""
    from teleshield import ml
    p = tmp_path / "m.json"
    model = ml.NaiveBayes()
    model.train([("加我微信穩賺", "spam"), ("你好在嗎", "legit")])
    model.save(p)
    monkeypatch.setattr(ml, "DEFAULT_MODEL_FILE", p)
    D._ML_CACHE.update({"mtime": None, "model": None})

    first = D.get_ml_model()
    assert first is not None
    import os
    import time
    time.sleep(0.01)
    os.utime(p, (time.time() + 5, time.time() + 5))  # 模擬重新訓練
    second = D.get_ml_model()
    assert second is not None
    assert D._ML_CACHE["mtime"] is not None


def test_ml_threshold_clamps():
    th = D.Thresholds(ml_block=5.0).clamp()
    assert th.ml_block == 1.0
    th = D.Thresholds(ml_block=-1.0).clamp()
    assert th.ml_block == 0.0


def test_default_thresholds_unchanged_by_ml_field():
    """★ 加欄位不能動到已經校準過的三個值。"""
    th = D.Thresholds()
    assert (th.auto_block, th.auto_allow, th.review) == (0.40, 0.70, 0.65)
    assert th.ml_block == 0.90
