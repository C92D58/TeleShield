"""本機樸素貝氏垃圾分類器（Multinomial NB）— 零依賴、可離線、可序列化。

## 它在管線裡的哪一格

- `patterns.py` 的正則：最便宜，但只認得字面。
- `decide.py` 的語意層（Jev）：看得懂，但要金鑰、要連線、要花錢。

中間還有一格：**規則沒抓到、又不值得花一次 API 呼叫**的訊息。
這支就是那一格——先在本地給一個 P(spam)。

呼叫端的正確用法是：

    p = score(text)          # 回 None 就代表這一層不可用
    if p is None:            # 沒有模型／模型壞掉 ✗ 走原本的路
        ...

★ `score()` 回 `None` **不是**「這是正常訊息」✗ 是「這一層沒有意見」。
  兩者混用會把廣告放行 ✗ 呼叫端必須自己分辨。

## 為什麼是樸素貝氏

- 訓練是一次計數，數千筆毫秒級，CPU 上就能跑。
- 有拉普拉斯平滑 ✗ 沒見過的詞不會把機率打成 0。
- 模型就是幾張計數表 ✗ 可以存成 JSON ✗ 使用者能自己看、自己刪。

## 為什麼 tokenizer 要自己寫

中文沒有空白可分 ✗ 而加 jieba 之類的分詞套件就違反了「只准用標準庫」。
這裡用**字元 bigram + 英文單詞**的混合：中文連續段取每個相鄰字對
（「加我微信」→ 加我 / 我微 / 微信），拉丁字母與數字取單詞。
bigram 對中文夠用，而且不需要任何詞典——「稳赚」「刷单」這種詞
會自然變成一個特徵。

★ 長度單位：長數字串（≥ `_MIN_DIGITS` 位，電話／QQ／微信號）正規化成 `<NUM>` ✗
  網址（含 `t.me/...`）正規化成 `<URL>` ✗ 否則每個廣告的網址都不同 ✗
  同一個模式會被拆成上百個一次性特徵 ✗ 什麼都學不到。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import HOME_DIR, load_block_log

__all__ = [
    "DEFAULT_MODEL_FILE",
    "LABELLED_FIXTURE",
    "NaiveBayes",
    "load_model",
    "score",
    "tokenize",
    "train_model",
    "training_samples",
]

DEFAULT_MODEL_FILE = HOME_DIR / "ml_model.json"

# 標註集（spam / legit 兩類，人工標的）。這是訓練資料裡唯一「乾淨」的來源 ✗
# block_log 只有 spam 一類，單靠它訓練出來的模型會永遠說 spam。
LABELLED_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "labelled_cases.json"

SPAM = "spam"
LEGIT = "legit"

# ── 正規化 ──────────────────────────────────────────────────────
# ★ 順序有意義：先吃網址再吃數字 ✗ 否則 URL 裡的數字會被先換成 <NUM>。
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"           # 完整網址
    r"|(?:t\.me|telegram\.me)/\S+"      # Telegram 連結（常沒有 scheme）
    r"|(?:tg://)\S+",
    re.IGNORECASE,
)
_MIN_DIGITS = 4
_NUM_RE = re.compile(r"\d{%d,}" % _MIN_DIGITS)

# 漢字範圍（含擴充 A 與相容表意文字）
_HAN = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_TOKEN_RE = re.compile(
    "|".join([
        r"(?P<placeholder><URL>|<NUM>)",        # 正規化後的佔位符（要放在最前面）
        r"(?P<han>[%s]+)" % _HAN,               # 中文連續段
        r"(?P<latin>[A-Za-z][A-Za-z0-9_]*)",    # 英文／數字單詞
        r"(?P<num>\d+)",                        # 沒被換掉的短數字
    ])
)


def _normalize(text: str) -> str:
    """把「每個廣告都不一樣」的字串收斂成固定佔位符。"""
    text = _URL_RE.sub(" <URL> ", text)
    return _NUM_RE.sub(" <NUM> ", text)


def tokenize(text: str) -> list[str]:
    """把訊息切成特徵。零依賴、無詞典，中英混排都適用。

    規則：
    - 中文連續段 → 每個字（unigram）＋ 每個相鄰字對（bigram）
    - 拉丁字母／數字 → 轉小寫的單詞
    - 網址 → `<URL>`；長數字串（≥4 位）→ `<NUM>`
    - 標點與 emoji **不是特徵**（它們對垃圾與正常訊息的分佈差不多）

    ★ 空白、`None` 之類的輸入不拋例外 ✗ 回空清單 ✗ 呼叫端不必自己防。

    >>> tokenize("加我微信 xiaoming888")
    ['加', '我', '微', '信', '加我', '我微', '微信', 'xiaoming888']
    >>> tokenize("快来 https://evil.com")
    ['快', '来', '快来', '<URL>']
    """
    if not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(_normalize(str(text))):
        placeholder = m.group("placeholder")
        if placeholder:
            out.append(placeholder)
            continue
        han = m.group("han")
        if han:
            # 單字保留（「赌」單獨出現也算證據），相鄰字對補上詞彙層級的訊號
            out.extend(han)
            out.extend(han[i:i + 2] for i in range(len(han) - 1))
            continue
        latin = m.group("latin")
        if latin:
            out.append(latin.lower())
            continue
        out.append(m.group("num"))
    return out


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子寫入（tmp + rename）並收緊權限 600。

    與 `config._atomic_write` 同手法，但這裡自己實作 ✗ 不依賴那支的私有符號。
    模型裡有使用者的訊息詞頻 ✗ 就算不是憑證也不該給別人看。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


class NaiveBayes:
    """多項式樸素貝氏 + 拉普拉斯平滑 + log 空間計算。

    純 Python，數千筆樣本 1 秒內可訓練完；模型可存成 JSON。

    ★ 為什麼全部用 log：一則訊息幾十個特徵 ✗ 每個機率都 < 1 ✗
      連乘會下溢成 0.0（浮點數的極限約 1e-308）✗ 於是每則都變成「不確定」。
      在 log 空間加總，最後才用 log-sum-exp 收回機率空間。

    ★ 兩類是**最低要求**：只有一類的模型 predict 會退化成長期常數
      （永遠 spam 或永遠 legit），那不是模型 ✗ 所以 `train()` 直接拒絕。
    """

    VERSION = 1

    def __init__(self, *, alpha: float = 1.0, min_df: int = 1):
        """
        alpha  ：拉普拉斯平滑係數（>0）。越大越保守 ✗ 沒見過的詞越不會被判死。
        min_df ：一個特徵至少要在幾則訊息裡出現過才留下來。1 = 全留。
                 ★ 出現一次的特徵幾乎都是雜訊（某個人的暱稱、某個一次性網址）
                   ✗ 它們會讓模型記住個案而不是模式 ✗ 用 2 過濾是最便宜的清理。
        """
        self.alpha = float(alpha)
        self.min_df = max(1, int(min_df))
        if self.alpha <= 0:
            raise ValueError("alpha 必須 > 0（否則未見過的詞機率會是 0）")
        self._classes: list[str] = []
        self._log_prior: dict[str, float] = {}
        self._vocab: list[str] = []
        self._vocab_set: set[str] = set()
        self._log_likelihood: dict[str, dict[str, float]] = {}
        self._n_samples = 0

    # ── 訓練 ────────────────────────────────────────────────
    def train(self, samples: list[tuple[str, str]]) -> "NaiveBayes":
        """用 `[(text, label)]` 訓練。label 通常是 "spam"/"legit"，其他值會自動收集。

        空訊息、空標籤會被跳過（不是錯誤 ✗ block_log 裡什麼都可能出現）。
        """
        docs: list[tuple[str, str]] = []
        for item in samples or []:
            try:
                text, label = item
            except (TypeError, ValueError):
                raise ValueError("樣本必須是 (text, label) 兩元組，收到：%r" % (item,)) from None
            text = "" if text is None else str(text)
            label = str(label).strip() if label is not None else ""
            if not label or not text.strip():
                continue
            docs.append((text, label))

        if not docs:
            raise ValueError("沒有任何可用的訓練樣本（空訊息與空標籤會在這裡被濾掉）")

        classes = sorted({label for _, label in docs})
        if len(classes) < 2:
            raise ValueError(
                "訓練樣本只有一類（%s）✗ 這種模型 predict 會退化成永遠 %s ✗ 拒絕訓練。"
                "要把 block_log（只有 spam）配上標註集（legit）才有效。" % (classes[0], classes[0])
            )

        n_docs = len(docs)
        doc_freq: Counter[str] = Counter()
        per_class: dict[str, Counter[str]] = {c: Counter() for c in classes}
        per_class_docs: Counter[str] = Counter()
        for text, label in docs:
            toks = tokenize(text)
            doc_freq.update(set(toks))
            per_class[label].update(toks)
            per_class_docs[label] += 1

        vocab = sorted(t for t, c in doc_freq.items() if c >= self.min_df)
        v_size = len(vocab)
        log_likelihood: dict[str, dict[str, float]] = {}
        for c in classes:
            counts = per_class[c]
            total = sum(counts[t] for t in vocab)
            denom = total + self.alpha * v_size
            if denom <= 0 or v_size == 0:
                log_likelihood[c] = {}
                continue
            # 拉普拉斯平滑：分子 +alpha ✗ 分母 +alpha*|V| ✗ 沒見過的詞也有機率
            log_likelihood[c] = {t: math.log((counts[t] + self.alpha) / denom) for t in vocab}

        self._classes = classes
        self._log_prior = {c: math.log(per_class_docs[c] / n_docs) for c in classes}
        self._vocab = vocab
        self._vocab_set = set(vocab)
        self._log_likelihood = log_likelihood
        self._n_samples = n_docs
        return self

    # ── 推論 ────────────────────────────────────────────────
    def _require_trained(self) -> None:
        if not self._classes:
            raise ValueError("模型還沒訓練過（或載入失敗）✗ 先呼叫 train()")

    def predict_proba(self, text: str) -> dict[str, float]:
        """回傳 {label: 機率}，和為 1。

        ★ 沒見過的詞是「沒有證據」而不是「0 機率」：以拉普拉斯平滑來看，
          每個類別都給它同一個 alpha/(total+alpha*|V|) ✗ 屬於常數 ✗
          對 argmax 與正規化後的機率完全沒有影響 ✗ 所以直接跳過不計。
        """
        self._require_trained()
        tokens = [t for t in tokenize(text or "") if t in self._vocab_set]

        scores: dict[str, float] = {}
        for c in self._classes:
            s = self._log_prior[c]
            table = self._log_likelihood.get(c, {})
            for t in tokens:
                lp = table.get(t)
                if lp is not None:
                    s += lp
            scores[c] = s

        # log-sum-exp：先減最大值再取 exp ✗ 避免 exp(-1000) 下溢成 0（那會變成 NaN）
        top = max(scores.values())
        exp_scores = {c: math.exp(s - top) for c, s in scores.items()}
        total = sum(exp_scores.values())
        if total <= 0 or not math.isfinite(total):  # pragma: no cover - 防禦性
            uniform = 1.0 / len(self._classes)
            return {c: uniform for c in self._classes}
        return {c: exp_scores[c] / total for c in self._classes}

    def predict(self, text: str) -> str:
        """argmax。平手時取排序後的第一個 ✗ 同樣輸入永遠給同樣答案。"""
        probs = self.predict_proba(text)
        return max(sorted(probs), key=lambda c: probs[c])

    # ── 屬性 ────────────────────────────────────────────────
    @property
    def classes(self) -> list[str]:
        """訓練時看到的標籤（已排序，保證可重現）。"""
        return list(self._classes)

    @property
    def vocab_size(self) -> int:
        """過完 min_df 之後留下的特徵數。"""
        return len(self._vocab)

    @property
    def n_samples(self) -> int:
        """實際參與訓練的訊息數（空訊息不計）。"""
        return self._n_samples

    # ── 序列化 ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": self.VERSION,
            "created": datetime.now(timezone.utc).isoformat(),
            "alpha": self.alpha,
            "min_df": self.min_df,
            "n_samples": self._n_samples,
            "classes": self._classes,
            "log_prior": self._log_prior,
            "vocab": self._vocab,
            "log_likelihood": self._log_likelihood,
        }

    def save(self, path: Path | None = None) -> Path:
        """原子寫入（tmp + rename）+ chmod 600，回傳實際寫入的路徑。"""
        target = Path(path) if path is not None else DEFAULT_MODEL_FILE
        _atomic_write_json(target, self.to_dict())
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "NaiveBayes | None":
        """讀模型。檔案不存在、不是 JSON、或形狀不對 → 回 `None`，**不拋例外**。

        ★ 這裡刻意寬容：模型是「有更好、沒有也能跑」的一層 ✗
          一個壞掉的模型檔不該讓整條封鎖管線掛掉。
        """
        target = Path(path) if path is not None else DEFAULT_MODEL_FILE
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict):
            return None

        try:
            classes = sorted(str(c) for c in raw["classes"])
            log_prior = {str(k): float(v) for k, v in raw["log_prior"].items()}
            vocab = [str(t) for t in raw["vocab"]]
            log_likelihood = {
                str(k): {str(t): float(v) for t, v in table.items()}
                for k, table in raw["log_likelihood"].items()
            }
            alpha = float(raw.get("alpha", 1.0))
            min_df = max(1, int(raw.get("min_df", 1)))
            n_samples = int(raw.get("n_samples", 0))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

        if len(classes) < 2:
            return None
        if set(log_prior) != set(classes) or set(log_likelihood) != set(classes):
            return None

        model = cls(alpha=alpha, min_df=min_df)
        model._classes = classes
        model._log_prior = log_prior
        model._vocab = vocab
        model._vocab_set = set(vocab)
        model._log_likelihood = log_likelihood
        model._n_samples = n_samples
        return model


# ════════════════════════════════════════════════════════════════════
# 資料收集
# ════════════════════════════════════════════════════════════════════
def _labelled_samples() -> list[tuple[str, str]]:
    """讀人工標註集。檔案不存在／壞掉／形狀不對 → 空清單（不拋）。"""
    try:
        data = json.loads(LABELLED_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        return []
    out: list[tuple[str, str]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        text, label = case.get("text"), case.get("label")
        if isinstance(text, str) and text.strip() and isinstance(label, str) and label.strip():
            out.append((text, label.strip()))
    return out


def training_samples(
    *,
    include_labelled: bool = True,
    include_block_log: bool = True,
    recent: int = 500,
    include_reason_fallback: bool = False,
) -> list[tuple[str, str]]:
    """收集訓練樣本，兩種來源合起來才有兩類：

    - block_log.json：`label="spam"`。**只取有 `"text"` 欄位的紀錄**。
    - `tests/fixtures/labelled_cases.json`：人工標註，用它自己的標籤（spam / legit）。

    ★ `include_reason_fallback` 預設是 **False**，這是刻意的：
      block_log 的 `reason` 是「severe 正則命中」這種**標籤**，不是訊息原文。
      拿它當特徵訓練等於在教模型背標籤 ✗ 準確率會漂亮得毫無意義
      （因為同一批 reason 又會出現在預測的特徵裡）。
      只有在你的 block_log 真的存了訊息原文、但欄位名不叫 text 時才打開它，
      而且打開後務必自己看一眼那些樣本是不是真的像訊息。

    ★ 一類的樣本湊不出模型 ✗ 所以這裡就擋掉：
      - 完全沒有樣本 → ValueError
      - 只有一類 → ValueError（訊息會說清楚缺哪一邊）

    這樣做的理由：只有 spam 的模型 predict 永遠回 spam ✗
    它「看起來會動」✗ 但沒有任何判斷力 ✗ 是最容易上線後才發現的那種問題。
    """
    samples: list[tuple[str, str]] = []

    if include_labelled:
        samples.extend(_labelled_samples())

    if include_block_log:
        blocks = load_block_log().get("blocks")
        if not isinstance(blocks, list):
            blocks = []
        for entry in blocks[-max(0, int(recent)):]:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                if not include_reason_fallback:
                    continue
                text = entry.get("reason")
            if isinstance(text, str) and text.strip():
                samples.append((text.strip(), SPAM))

    if not samples:
        raise ValueError("沒有任何訓練樣本 ✗ block_log 沒有 text 欄位、標註集也讀不到")

    labels = {label for _, label in samples}
    if len(labels) < 2:
        raise ValueError(
            "訓練樣本只有一類（%s）✗ 這樣訓練出來的模型只會給同一個答案 ✗ 拒絕產出。"
            "需要另一類的樣本：標註集測 spam / legit 都有，block_log 只有 spam。"
            % "、".join(sorted(labels))
        )
    return samples


# ════════════════════════════════════════════════════════════════════
# 對外：訓練 / 載入 / 打分
# ════════════════════════════════════════════════════════════════════
def train_model(samples: list[tuple[str, str]] | None = None, *, path: Path | None = None) -> dict:
    """訓練、存檔，回傳統計數字。

    `samples` 省略時用 `training_samples()` 收集（block_log + 標註集）。

    ★ `accuracy` 是**訓練集本身的準確率**，不是泛化能力。
      它只說明「模型有沒有把訓練資料記起來」，通常高得沒有意義，
      **不要**拿它當成果對外宣傳。要看真實表現只能用沒參與訓練的樣本，
      而目前手上的樣本量（數十筆）還不足以切出可靠的測試集。

    回傳：`n_samples` / `n_spam` / `n_legit` / `vocab` / `path` / `accuracy`。
    """
    data = list(samples) if samples is not None else training_samples()
    if not data:
        raise ValueError("沒有任何訓練樣本 ✗ 拒絕產出一個空模型")

    model = NaiveBayes().train(data)

    target = Path(path) if path is not None else DEFAULT_MODEL_FILE
    model.save(target)

    correct = sum(1 for text, label in data if model.predict(text) == label)
    return {
        "n_samples": model.n_samples,
        "n_spam": sum(1 for _, label in data if str(label).strip() == SPAM),
        "n_legit": sum(1 for _, label in data if str(label).strip() == LEGIT),
        "vocab": model.vocab_size,
        "path": str(target),
        "accuracy": correct / len(data),  # 訓練集準確率，不是泛化能力
    }


def load_model(path: Path | None = None) -> NaiveBayes | None:
    """載入模型。沒有或壞掉 → `None`（呼叫端據此決定這層要不要用）。"""
    return NaiveBayes.load(path)


def score(text: str, model: NaiveBayes | None = None) -> float | None:
    """回傳 P(spam)（0-1）。

    - 沒有模型（或模型壞掉、或模型裡根本沒有 spam 這一類）→ `None`。
      ★ `None` 是「這一層沒有意見」✗ 不是「正常訊息」✗ 呼叫端要能分辨。
    - `model` 省略時會嘗試從 `DEFAULT_MODEL_FILE` 讀（每次呼叫都讀 ✗
      呼叫端要大量打分就自己 `load_model()` 一次、把模型傳進來）。
    """
    m = model if model is not None else load_model()
    if m is None:
        return None
    try:
        probs = m.predict_proba(text or "")
    except ValueError:
        return None
    return probs.get(SPAM)
