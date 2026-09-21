"""本機樸素貝氏分類器的行為測試。

★ 這裡驗的不是準確率 ✗ 是**行為**。

理由：手上的樣本只有數十筆 ✗ 從它們算出來的任何準確率都不可信
（模型記得住訓練集，但那不代表會推廣）。真正會上線後出事的是別的東西：

- 中文沒有空白可分 ✗ 切不出特徵的分類器會靜靜地永遠回同一個答案
- 只有一類樣本時偷偷產出一個垃圾模型
- 沒見過的詞讓機率下溢成 0 或 NaN
- 存下去的模型讀不回來、或讀壞檔直接讓整條管線炸掉

所以下面每一條都對應一個「不該發生的具體結果」，而不是分數漂不漂亮。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from teleshield import ml

# ── 測試語料（刻意寫死 ✗ 不依賴使用者機器上的 block_log）────────────
SPAM_SAMPLES = [
    ("加我微信 abc123", "spam"),
    ("兼职刷单 日入500", "spam"),
    ("投资理财稳赚 高回报", "spam"),
    ("博彩 六合彩 投注", "spam"),
    ("免费领取红包", "spam"),
]
LEGIT_SAMPLES = [
    ("你好，最近怎么样？", "legit"),
    ("这个周末我们出去玩吧", "legit"),
    ("好的收到", "legit"),
    ("今天加班", "legit"),
    ("谢谢你的分享", "legit"),
]


@pytest.fixture
def corpus():
    return list(SPAM_SAMPLES + LEGIT_SAMPLES)


@pytest.fixture
def model(corpus):
    return ml.NaiveBayes().train(corpus)


@pytest.fixture
def tmp_model_path(tmp_path, monkeypatch):
    """把預設模型路徑指向 tmp ✗ 測試不准碰使用者真的 ~/.teleshield。"""
    path = tmp_path / "ml_model.json"
    monkeypatch.setattr(ml, "DEFAULT_MODEL_FILE", path)
    return path


@pytest.fixture
def quiet_block_log(monkeypatch):
    """空的 block_log（不存在 text 欄位是常態）。"""
    monkeypatch.setattr(ml, "load_block_log", lambda: {"blocks": []})
    return []


@pytest.fixture
def mini_labelled_fixture(tmp_path, monkeypatch):
    """只有一筆 legit 的標註集。

    用途：`training_samples()` 會拒絕單類樣本 ✗ 所以要測 block_log 的收樣邏輯時，
    得先確保 legit 那邊有東西，否則測到的是別人的 ValueError。
    """
    path = tmp_path / "labelled_cases.json"
    path.write_text(json.dumps({"cases": [{"text": "好的收到", "label": "legit"}]}), encoding="utf-8")
    monkeypatch.setattr(ml, "LABELLED_FIXTURE", path)
    return path


# ════════════════════════════════════════════════════════════════════
# tokenize
# ════════════════════════════════════════════════════════════════════
class TestTokenize:
    def test_chinese_produces_bigrams(self):
        """★ 中文沒有空白 ✗ bigram 是零依賴下唯一能表達詞彙的手段。"""
        tokens = ml.tokenize("加我微信")
        assert tokens == ["加", "我", "微", "信", "加我", "我微", "微信"]
        assert "微信" in tokens, "「微信」是關鍵詞，切不出來就等於這層沒用"

    def test_bigram_does_not_cross_punctuation(self):
        # 「加我」與「微信」之間有標點 ✗ 不該生出「我，微」這種跨界的假 bigram
        tokens = ml.tokenize("加我，微信")
        assert "加我" in tokens and "微信" in tokens
        assert "我，微" not in tokens and "我微" not in tokens

    def test_latin_is_lowercased(self):
        assert ml.tokenize("FREE Bitcoin") == ["free", "bitcoin"]

    def test_url_normalised_to_placeholder(self):
        """★ 每個廣告的網址都不同 ✗ 不正規化就只會學到上百個一次性特徵。"""
        tokens = ml.tokenize("快来 https://evil.com/promo?x=1")
        assert "<URL>" in tokens
        assert "evil" not in tokens and "https" not in tokens

    def test_telegram_link_normalised_without_scheme(self):
        assert ml.tokenize("t.me/joinchat/xxx") == ["<URL>"]

    def test_long_number_normalised_to_placeholder(self):
        tokens = ml.tokenize("联系 13800138000 加我")
        assert "<NUM>" in tokens
        assert "13800138000" not in tokens

    def test_short_number_stays_a_token(self):
        # 門檻是 4 位 ✗ 「50」「500」這種短數字留著當一般特徵
        assert "50" in ml.tokenize("limited offer 50% off")

    def test_falsy_input_returns_empty_list(self):
        assert ml.tokenize("") == []
        assert ml.tokenize("   ") == []
        assert ml.tokenize(None) == []  # type: ignore[arg-type]

    def test_punctuation_and_emoji_are_not_features(self):
        assert ml.tokenize("！！！🎉🎉") == []


# ════════════════════════════════════════════════════════════════════
# 訓練的失敗路徑
# ════════════════════════════════════════════════════════════════════
class TestTrainingRefusesJunk:
    def test_train_rejects_single_class(self, corpus):
        """★ 只有 spam 的模型 predict 永遠回 spam ✗ 看起來會動 ✗ 但沒有判斷力。"""
        with pytest.raises(ValueError, match="只有一類"):
            ml.NaiveBayes().train(SPAM_SAMPLES)

    def test_train_rejects_empty_samples(self):
        with pytest.raises(ValueError):
            ml.NaiveBayes().train([])

    def test_train_model_rejects_single_class_and_writes_nothing(self, tmp_model_path):
        with pytest.raises(ValueError, match="只有一類"):
            ml.train_model(SPAM_SAMPLES)
        assert not tmp_model_path.exists(), "寧可沒有模型 ✗ 也不要留一個垃圾模型在磁碟上"

    def test_training_samples_raises_when_only_one_class(self, monkeypatch):
        """block_log 只有 spam ✗ 沒配標註集就該當場報錯，不是默默回一疊單類樣本。"""
        monkeypatch.setattr(
            ml, "load_block_log", lambda: {"blocks": [{"text": "加我微信 abc", "reason": "severe 正則命中"}]}
        )
        with pytest.raises(ValueError, match="只有一類"):
            ml.training_samples(include_labelled=False)

    def test_training_samples_raises_without_any_source(self, monkeypatch):
        monkeypatch.setattr(ml, "load_block_log", lambda: {"blocks": []})
        monkeypatch.setattr(ml, "LABELLED_FIXTURE", Path("/nonexistent/labelled_cases.json"))
        with pytest.raises(ValueError):
            ml.training_samples()


# ════════════════════════════════════════════════════════════════════
# 樣本收集
# ════════════════════════════════════════════════════════════════════
class TestTrainingSamples:
    def test_labelled_fixture_supplies_both_classes(self, quiet_block_log):
        samples = ml.training_samples()
        labels = {label for _, label in samples}
        assert labels == {"spam", "legit"}, "兩類都要有才有訓練價值"
        assert len(samples) >= 30

    def test_block_log_without_text_is_skipped_by_default(self, monkeypatch, mini_labelled_fixture):
        """★ reason 是標籤不是訊息原文 ✗ 拿它訓練等於在教模型背標籤。"""
        monkeypatch.setattr(
            ml, "load_block_log",
            lambda: {"blocks": [{"text": "加我微信 abc", "reason": "severe 正則命中"},
                                {"reason": "moderate 疊加計分"}]},
        )
        samples = ml.training_samples()  # include_reason_fallback 預設是 False
        assert ("加我微信 abc", "spam") in samples, "有 text 的紀錄照收"
        assert not any("疊加計分" in text for text, _ in samples), "沒有 text 就該整筆跳過"

    def test_block_log_without_text_used_only_when_explicitly_enabled(self, monkeypatch, mini_labelled_fixture):
        monkeypatch.setattr(
            ml, "load_block_log",
            lambda: {"blocks": [{"text": "加我微信 abc", "reason": "severe 正則命中"},
                                {"reason": "moderate 疊加計分"}]},
        )
        samples = ml.training_samples(include_reason_fallback=True)
        assert ("加我微信 abc", "spam") in samples, "有 text 就優先用 text ✗ 不是用 reason"
        assert ("moderate 疊加計分", "spam") in samples

    def test_training_samples_are_usable(self, quiet_block_log):
        model = ml.NaiveBayes().train(ml.training_samples())
        assert model.n_samples > 0 and model.vocab_size > 0


# ════════════════════════════════════════════════════════════════════
# 機率
# ════════════════════════════════════════════════════════════════════
class TestProbabilities:
    def test_obvious_spam_scores_high(self, model):
        p = model.predict_proba("加我微信 兼职刷单 稳赚")["spam"]
        assert p > 0.8, "明顯的廣告還判不出來，這層就沒有存在價值（實際 %.4f）" % p

    def test_normal_message_scores_low(self, model):
        p = model.predict_proba("你好，这个周末一起吃饭吧")["spam"]
        assert p < 0.2, "正常訊息被判成廣告比漏封更貴（實際 %.4f）" % p

    def test_proba_is_a_distribution(self, model):
        for text in ("加我微信", "好的收到", "完全沒見過的字串", "", "🎉"):
            probs = model.predict_proba(text)
            assert set(probs) == set(model.classes)
            assert all(0.0 <= v <= 1.0 for v in probs.values())
            assert sum(probs.values()) == pytest.approx(1.0)

    def test_empty_text_falls_back_to_priors(self, model):
        probs = model.predict_proba("")
        assert probs == pytest.approx({"spam": 0.5, "legit": 0.5}), "兩類各 5 筆 ✗ 沒有證據時就該回到先驗"
        assert model.predict_proba("   ") == pytest.approx(probs)

    def test_unseen_words_do_not_blow_up(self, model):
        """★ 拉普拉斯平滑生效：沒見過的詞不能讓機率變 0、1 或 NaN。"""
        probs = model.predict_proba("阿爾發半人馬座 zzzz qqqq 9876543210")
        assert all(math.isfinite(v) and v > 0.0 for v in probs.values()), probs
        assert sum(probs.values()) == pytest.approx(1.0)

    def test_extreme_inputs_do_not_raise(self, model):
        for text in ("！！！", "🎉🎉🎉", "", " ", "\n\t", "a" * 20000, "中" * 5000, "@#$%^&*()"):
            probs = model.predict_proba(text)
            assert all(math.isfinite(v) for v in probs.values()), repr(text[:20])
            assert sum(probs.values()) == pytest.approx(1.0)

    def test_predict_returns_a_trained_class(self, model):
        assert model.predict("加我微信 兼职刷单") == "spam"
        assert model.predict("好的收到") == "legit"
        assert model.predict("") in model.classes

    def test_properties_describe_the_training_set(self, model, corpus):
        assert model.classes == ["legit", "spam"], "排序過才可重現"
        assert model.n_samples == len(corpus)
        assert model.vocab_size > 0

    def test_untrained_model_refuses_to_predict(self):
        with pytest.raises(ValueError):
            ml.NaiveBayes().predict_proba("加我微信")


# ════════════════════════════════════════════════════════════════════
# min_df
# ════════════════════════════════════════════════════════════════════
MIN_DF_SAMPLES = [
    ("刷单 兼职", "spam"),
    ("兼职 刷单 加我", "spam"),
    ("獨一無二的雜訊詞", "spam"),   # 只在這一則出現 ✗ 就是 min_df 要清掉的東西
    ("你好 收到", "legit"),
    ("今天 加班 收到", "legit"),
]


class TestMinDf:
    def test_min_df_filters_singleton_noise(self, tmp_path):
        p1 = tmp_path / "keep_all.json"
        p2 = tmp_path / "min_df_2.json"
        ml.NaiveBayes(min_df=1).train(MIN_DF_SAMPLES).save(p1)
        ml.NaiveBayes(min_df=2).train(MIN_DF_SAMPLES).save(p2)
        vocab_all = json.loads(p1.read_text(encoding="utf-8"))["vocab"]
        vocab_df2 = json.loads(p2.read_text(encoding="utf-8"))["vocab"]

        assert "獨" in vocab_all and "雜訊" in vocab_all, "min_df=1 時什麼都不該濾"
        assert "獨" not in vocab_df2 and "雜訊" not in vocab_df2, "只出現一次的特徵該被濾掉"
        assert "兼职" in vocab_df2, "出現兩次的特徵要留下來（濾的是雜訊不是罕見詞）"
        assert len(vocab_df2) < len(vocab_all)

    def test_min_df_2_still_trains_both_classes(self, tmp_path):
        model = ml.NaiveBayes(min_df=2).train(MIN_DF_SAMPLES)
        assert model.classes == ["legit", "spam"]
        assert sum(model.predict_proba("兼职 刷单").values()) == pytest.approx(1.0)

    def test_invalid_alpha_rejected(self):
        with pytest.raises(ValueError):
            ml.NaiveBayes(alpha=0)


# ════════════════════════════════════════════════════════════════════
# 序列化
# ════════════════════════════════════════════════════════════════════
class TestPersistence:
    def test_save_load_roundtrip(self, model, tmp_path):
        path = model.save(tmp_path / "m.json")
        loaded = ml.NaiveBayes.load(path)
        assert loaded is not None
        assert loaded.classes == model.classes
        assert loaded.vocab_size == model.vocab_size
        assert loaded.n_samples == model.n_samples
        for text in ("加我微信 兼职刷单", "你好，这个周末一起吃饭吧", "完全沒見過"):
            assert loaded.predict_proba(text) == pytest.approx(model.predict_proba(text))

    def test_save_uses_default_path_and_private_mode(self, model, tmp_model_path):
        path = model.save()
        assert path == tmp_model_path and path.exists()
        assert path.stat().st_mode & 0o777 == 0o600, "模型含使用者的訊息詞頻 ✗ 別人也別想看"
        assert not path.with_suffix(".json.tmp").exists(), "原子寫入不該留下暫存檔"
        assert ml.load_model() is not None, "load() 不給路徑時要讀預設路徑"

    def test_load_missing_file_returns_none(self, tmp_path):
        assert ml.NaiveBayes.load(tmp_path / "does_not_exist.json") is None
        assert ml.load_model(tmp_path / "does_not_exist.json") is None

    def test_load_corrupt_json_returns_none(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        assert ml.NaiveBayes.load(bad) is None

    @pytest.mark.parametrize(
        "payload",
        [
            [],                                              # 不是物件
            {"classes": ["spam"]},                           # 只有一類
            {"classes": ["spam", "legit"]},                  # 缺表格
            {"classes": ["spam", "legit"], "log_prior": {}, "vocab": [], "log_likelihood": {"spam": {}}},
            {"classes": ["spam", "legit"], "log_prior": {"spam": "x"}, "vocab": [], "log_likelihood": {}},
            {"classes": "spam", "log_prior": None, "vocab": None, "log_likelihood": None},
        ],
    )
    def test_load_wrong_shape_returns_none(self, tmp_path, payload):
        """★ 壞檔回 None 而不是拋例外：一個壞模型不該讓封鎖管線掛掉。"""
        bad = tmp_path / "shape.json"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        assert ml.NaiveBayes.load(bad) is None

    def test_load_empty_file_returns_none(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        assert ml.NaiveBayes.load(empty) is None


# ════════════════════════════════════════════════════════════════════
# train_model / score
# ════════════════════════════════════════════════════════════════════
class TestTrainModelAndScore:
    def test_train_model_returns_counts_and_writes_file(self, corpus, tmp_path):
        path = tmp_path / "trained.json"
        stats = ml.train_model(corpus, path=path)
        assert stats["n_samples"] == len(corpus)
        assert stats["n_spam"] == len(SPAM_SAMPLES)
        assert stats["n_legit"] == len(LEGIT_SAMPLES)
        assert stats["vocab"] > 0
        assert stats["path"] == str(path)
        assert 0.0 <= stats["accuracy"] <= 1.0
        assert ml.NaiveBayes.load(path) is not None

    def test_accuracy_is_documented_as_training_set_only(self):
        """★ 不准把訓練集準確率當成績宣傳 ✗ 所以明文寫在 docstring 裡。"""
        doc = ml.train_model.__doc__ or ""
        assert "訓練集" in doc and "泛化" in doc

    def test_train_model_without_samples_uses_collected_data(self, quiet_block_log, tmp_path, monkeypatch):
        monkeypatch.setattr(ml, "DEFAULT_MODEL_FILE", tmp_path / "auto.json")
        stats = ml.train_model()
        assert stats["n_spam"] > 0 and stats["n_legit"] > 0
        assert (tmp_path / "auto.json").exists()

    def test_score_returns_none_without_model(self, tmp_model_path):
        """★ 沒模型是「這層沒意見」✗ 不是「這是正常訊息」✗ 呼叫端要自己分辨。"""
        assert ml.score("加我微信 兼职刷单") is None

    def test_score_returns_spam_probability(self, model):
        p = ml.score("加我微信 兼职刷单", model=model)
        assert p is not None and p > 0.8
        assert p == pytest.approx(model.predict_proba("加我微信 兼职刷单")["spam"])

    def test_score_with_model_missing_spam_class_returns_none(self):
        model = ml.NaiveBayes().train([("hello there", "greeting"), ("hello world", "greeting2")])
        assert ml.score("hello", model=model) is None

    def test_score_loads_from_default_path(self, model, tmp_model_path):
        model.save()
        p = ml.score("加我微信 兼职刷单")
        assert p is not None and p > 0.8
