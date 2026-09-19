"""決定層：便宜的規則先篩，語意判斷才交給 System One（Jev）。

## 為什麼是「並行」而不是「替換」

`patterns.py` 的正則不是要淘汰的東西，它是**最便宜的那一層**：
命中 severe 就沒有必要花一次 API 呼叫去確認。反過來，正則永遠解不了
「你好，想問你們接不接企業內訓」這種句子——沒有關鍵詞，但也不是廣告。

所以順序是：

    severe 正則命中        → 直接封，不呼叫 API          （最便宜、最確定）
    疊加計分 ≥ 3           → 直接封，不呼叫 API
    其餘                   → 交給 Jev 做語意判斷
    Jev 信心 < 自動門檻    → 進人工佇列

只有中間那段模糊地帶需要花錢，而它正好是正則最沒用的地方。

## 沒有金鑰時會怎樣

`StubJudge` 是**離線的替代品**，用既有規則模擬同樣形狀的答案，
好處是整條管線（含測試）在拿到 API key 之前就能跑完。

它**不是**模型判斷，所以 `Decision.source` 會是 `"stub"`——**不要**
把 stub 的結果當成 Jev 的結果。要接真的，設 `TYPESAFE_API_KEY` 即可。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .patterns import match_patterns

__all__ = [
    "Action",
    "Decision",
    "Judge",
    "JevJudge",
    "StubJudge",
    "Thresholds",
    "build_questions",
    "decide",
    "get_judge",
    "SYSTEMONE_ENDPOINT",
    "DEFAULT_MODEL",
]

SYSTEMONE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

Action = Literal["block", "review", "allow"]


# ════════════════════════════════════════════════════════════════════
# 門檻：三個數字決定一切，所以要能從 config 覆寫
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Thresholds:
    """決定「自動」與「人工」的分界。

    ★ 為什麼封鎖與放行**不能用同一個門檻**：

        「夠確定可以封」  和「夠確定可以不管」  是兩個不同的問題。

      封錯了會傷到真人（代價高）✗ 所以門檻要高；
      放行錯了只是少擋一則廣告（代價低）✗ 門檻可以低一點。
      用同一個數字 ✗ 結果就是 allow 幾乎永遠不觸發 ✗ 全部擠進人工。

    ★ 這三個數字**必須用你自己的標註資料定**，不要照抄。
      官方給的工具門檻（0.6）是在客服工單上調出來的，不是垃圾訊息。
      做法：拿 tests/ 裡那些案例當種子，標上「該封 / 不該封」，
      然後看不同門檻下的漏封與誤封，再挑一個你接受的點。

    auto_block ：信心高於此值才自動封（保守）
    auto_allow ：信心高於此值且模型說不需要人看 ✗ 才自動放行
    review     ：needs_human 高於此值就讓人看一眼
    """

    auto_block: float = 0.90
    auto_allow: float = 0.70
    review: float = 0.35

    def clamp(self) -> "Thresholds":
        a = min(max(self.auto_block, 0.0), 1.0)
        al = min(max(self.auto_allow, 0.0), 1.0)
        r = min(max(self.review, 0.0), 1.0)
        return Thresholds(auto_block=a, auto_allow=al, review=r)


# ════════════════════════════════════════════════════════════════════
# 結果
# ════════════════════════════════════════════════════════════════════
@dataclass
class Decision:
    # 預設是 review ✗ 不是 allow ✓ 未知狀態下保守的那一邊永遠是「讓人看一眼」
    action: Action = "review"
    source: Literal["regex", "jev", "stub", "error"] = "regex"
    kind: str | None = None            # spam / promo / scam / legit
    confidence: float | None = None    # Choice 的信心（分佈集中度）
    aggression: float | None = None    # Score 的期望值
    needs_human: float | None = None   # Noul 的機率
    tier: str | None = None            # 正則命中的最高嚴重級
    hits: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def __str__(self) -> str:
        bits = [self.action.upper(), "via " + self.source]
        if self.kind:
            bits.append(self.kind)
        if self.confidence is not None:
            bits.append("conf %.2f" % self.confidence)
        if self.note:
            bits.append("(" + self.note + ")")
        return " ".join(bits)


# ════════════════════════════════════════════════════════════════════
# 問題定義：★ 判斷寫在 instructions ✗ 可能答案寫在 criteria
#   不要把問題寫進 state——那會被當成「要被判斷的材料」本身
# ════════════════════════════════════════════════════════════════════
def build_questions() -> dict[str, Any]:
    return {
        "kind": {
            "type": "choice",
            "options": ["spam", "promo", "scam", "legit"],
            "instructions": (
                "判斷這則私訊的性質。只看訊息內容本身，不要因為對方是新帳號就改變判斷。"
            ),
            "criteria": {
                "spam": "批量發送的廣告或引流，內容與收件者無關，對方明顯在撒網",
                "promo": "主動推銷，但針對性明確、不是群發模板",
                "scam": "詐騙、賭博、色情、金融欺詐，或誘導匯款、下載、加私聊",
                "legit": "正常詢問、合作洽談、回覆既有往來，或無法歸類的普通訊息",
            },
        },
        "aggression": {
            "type": "score",
            "min": 0,
            "max": 100,
            "instructions": (
                "這則訊息對收件者施加壓力的程度。0 = 完全被動陳述；"
                "50 = 明確催促但留餘地；100 = 限時逼迫、反覆催促、製造恐懼。"
            ),
        },
        "needs_human": {
            "type": "noul",
            "instructions": (
                "這則訊息是否處於模糊地帶，讓人看一眼再決定比自動處理更安全？"
            ),
        },
    }


def build_state(text: str, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """state 給「材料」，問題給「判斷」。兩者不要混。"""
    state: dict[str, Any] = {"message": {"text": text}}
    if ctx:
        # 只帶真正有助判斷的欄位，不要把整個 config 倒進去
        for k in ("sender_name", "sender_username", "is_contact", "account_age_days",
                  "message_count", "has_link", "has_media", "recent_messages"):
            if k in ctx:
                state[k] = ctx[k]
    return state


# ════════════════════════════════════════════════════════════════════
# Judge：只有兩種實作
# ════════════════════════════════════════════════════════════════════
class Judge(Protocol):
    def judge(self, state: dict, questions: dict) -> dict:  # pragma: no cover
        ...


class JevJudge:
    """真正的 System One 呼叫。只用標準庫，不強迫裝 SDK。

    要 SDK 的型別推導就 `pip install typesafe-sdk-python`，但這支不需要。
    """

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 endpoint: str = SYSTEMONE_ENDPOINT, timeout: float = 10.0):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY", "")
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        if not self.api_key:
            raise ValueError(
                "沒有 TYPESAFE_API_KEY。Jev 還在早期訪問，先排 waitlist；"
                "在此之前用 StubJudge（offline=True）跑管線。"
            )

    def judge(self, state: dict, questions: dict) -> dict:
        body = json.dumps({"model": self.model, "state": state,
                           "questions": questions}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


class StubJudge:
    """離線替代品。**不是模型判斷**，只是用既有規則模擬同樣形狀的答案。

    存在的理由：讓整條管線（含 tests）在拿到 key 之前就能跑、
    能驗證組合邏輯、能先把門檻的框架搭好。
    """

    def judge(self, state: dict, questions: dict) -> dict:
        text = (state.get("message") or {}).get("text", "") or ""
        tier, hits = match_patterns(text)
        n = len(hits)

        if tier == "severe":
            kind, conf = "scam", 0.94
        elif tier == "moderate":
            kind, conf = ("spam", 0.62 + min(n, 4) * 0.05)
        elif tier == "low":
            kind, conf = ("promo", 0.48 + min(n, 3) * 0.06)
        else:
            kind, conf = "legit", 0.70
        conf = min(conf, 0.97)

        aggr = {"severe": 78, "moderate": 52, "low": 34}.get(tier or "", 18) + min(n * 2, 12)
        # 模糊地帶＝信心不高、或正則在中低危之間擺盪
        needs = 0.72 if conf < 0.60 else (0.42 if tier == "moderate" else 0.10)

        return {
            "kind": {"value": kind, "confidence": round(conf, 3),
                     "probabilities": {k: round(1 - conf, 3) for k in ("spam", "promo", "scam", "legit") if k != kind} | {kind: round(conf, 3)}},
            "aggression": {"value": min(aggr, 100)},
            "needs_human": {"probability": round(needs, 3)},
            "_stub": True,
        }


def get_judge(offline: bool = False, **kw) -> Judge:
    """挑一個 judge。沒有金鑰就自動退回離線版，不讓呼叫端炸掉。"""
    if offline or not os.getenv("TYPESAFE_API_KEY"):
        return StubJudge()
    return JevJudge(**kw)


# ════════════════════════════════════════════════════════════════════
# 組合：這是這支檔案存在的理由
# ════════════════════════════════════════════════════════════════════
# ★ 這三個讀取器必須忍受「形狀不對的回應」。
#   服務是多數時候正常、偶爾回一個字串或 null 的東西——那時候我們要進人工，
#   不是讓整條管線拋 AttributeError 掛掉。所以每一個都先確認容器型別。


def _as_dict(ans) -> dict:
    """把任何回應都收斂成 dict ✗ 不是 dict 就當空。"""
    if isinstance(ans, dict):
        return ans
    if isinstance(ans, (str, int, float, bool)) or ans is None:
        return {}
    try:
        return dict(ans)
    except (TypeError, ValueError):
        return {}


def _read_choice(ans) -> tuple[str | None, float | None]:
    a = _as_dict(ans)
    v = a.get("value") or a.get("choice") or a.get("selected")
    if not isinstance(v, str):
        v = None
    c = a.get("confidence")
    return v, (float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else None)


def _read_score(ans) -> float | None:
    a = _as_dict(ans)
    for k in ("value", "score", "expected"):
        v = a.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _read_noul(ans) -> float | None:
    a = _as_dict(ans)
    for k in ("probability", "value", "prob", "p"):
        v = a.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def decide(text: str, ctx: dict | None = None, judge: Judge | None = None,
           thresholds: Thresholds | None = None, cfg: dict | None = None,
           ) -> Decision:
    """一則訊息 → 一個決定。規則優先，語意補位，信心不足交人工。"""
    th = (thresholds or Thresholds()).clamp()

    if not text or not text.strip():
        return Decision(action="allow", source="regex", note="空訊息")

    # ── 第一層：便宜且確定的 ──────────────────────────────
    tier, hits = match_patterns(text)
    if tier == "severe":
        return Decision(action="block", source="regex", kind="scam",
                        tier=tier, hits=hits, note="severe 正則命中，未呼叫 API")

    if cfg:
        try:
            from .patterns import is_spam
            if is_spam(text, cfg):
                return Decision(action="block", source="regex", tier=tier or "low",
                                hits=hits, note="疊加計分達標，未呼叫 API")
        except Exception:  # 規則層壞掉不該讓整條管線掛掉
            pass

    # ── 第二層：語意判斷 ──────────────────────────────────
    j = judge or get_judge()
    try:
        raw = j.judge(build_state(text, ctx), build_questions())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        # ★ 服務掛掉不等於「放行」。語意層不可用時，保守地進人工。
        return Decision(action="review", source="error", tier=tier, hits=hits,
                        note="System One 呼叫失敗：%s" % type(e).__name__)
    except Exception as e:  # 回應形狀不如預期
        return Decision(action="review", source="error", tier=tier, hits=hits,
                        note="回應無法解析：%s" % type(e).__name__)

    if not isinstance(raw, dict):
        return Decision(source="error", tier=tier, hits=hits,
                        note="回應不是物件（%s）" % type(raw).__name__)
    is_stub = bool(raw.pop("_stub", False))
    source: Literal["jev", "stub"] = "stub" if is_stub else "jev"

    kind, conf = _read_choice(raw.get("kind") or {})
    aggr = _read_score(raw.get("aggression") or {})
    needs = _read_noul(raw.get("needs_human") or {})

    d = Decision(source=source, kind=kind, confidence=conf, aggression=aggr,
                 needs_human=needs, tier=tier, hits=hits, raw=raw)

    # ── 第三層：把機率變成行為 ────────────────────────────
    if kind in ("spam", "scam") and conf is not None and conf >= th.auto_block:
        d.action = "block"
    elif kind == "legit" and conf is not None and conf >= th.auto_allow and (needs or 0) < th.review:
        d.action = "allow"
    else:
        d.action = "review"

    if needs is not None and needs >= th.review and d.action != "block":
        d.action = "review"
        d.note = d.note or "模型自己說需要人看"

    if is_stub:
        d.note = (d.note + " " if d.note else "") + "離線 stub，不是 Jev 的判斷"

    return d
