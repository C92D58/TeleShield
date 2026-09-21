#!/usr/bin/env python3
"""用標註集量決定層，並掃門檻。

## 這個工具回答兩個問題

**一、規則層已經吃掉多少？**
    正則命中的部分是免費的 ✗ 剩下的才要花 API 錢 ✓
    所以先看「有多少 spam 是規則抓的、有多少得靠語意」✓
    這決定了你的帳單 ✗ 也決定了語意層到底值不值得接 ✓

**二、門檻該設在哪？**
    掃過一組候選門檻 ✗ 對每個門檻報四件事：
      漏封（spam 沒被封） ✗ 誤封（legit 被誤封 ✗ 代價最高 ✓）
      review 率（人工工作量 ✓） ✗ 省下的 API 呼叫 ✓

## ★ 關於「用 stub 跑出來的門檻」

`--offline` 用的是 `StubJudge` ✗ **那不是模型判斷** ✓
所以它跑出來的門檻**不能拿去用** ✗ 它只證明：
  · 這條管線會動
  · 門檻掃描的邏輯對
  · 評分標準（漏封/誤封/review 率）算得對

**真門檻要等 `TYPESAFE_API_KEY` 到位 ✗ 用同一支指令跑 ✗ 才是真的** ✓

用法：
    python3 tools/calibrate.py                        # 規則層 + stub 掃描
    python3 tools/calibrate.py --judge typesafe       # 真的有金鑰時（會花錢）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teleshield.decide import (  # noqa: E402
    StubJudge,
    Thresholds,
    _read_choice,
    _read_noul,
    _read_probs,
    build_questions,
    build_state,
    decide,
)
from teleshield.patterns import is_spam  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "labelled_cases.json"


def load_cases() -> list[dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["cases"]


# ════════════════════════════════════════════════════════════════
# 一、規則層單獨的表現
# ════════════════════════════════════════════════════════════════
def analyse_rules(cases: list[dict]) -> dict:
    """規則層不需要模型 ✗ 這一段的數字現在就是真的 ✓"""
    spam = [c for c in cases if c["label"] == "spam"]
    legit = [c for c in cases if c["label"] == "legit"]

    # ★ 用專案真正的規則判定 ✗ 不是只看 severe。
    #   patterns.is_spam 是「疊加計分 >= 3」✗ 會吃掉 moderate/low 的組合，
    #   只看 severe 會嚴重低估規則層的覆蓋率（實測 7 → 16）。
    spam_free = [c for c in spam if is_spam(c["text"])]
    spam_semantic = [c for c in spam if not is_spam(c["text"])]
    legit_blocked = [c for c in legit if is_spam(c["text"])]

    return {
        "spam_total": len(spam),
        "spam_caught_free": len(spam_free),
        "spam_to_semantic": len(spam_semantic),
        "legit_total": len(legit),
        "legit_wrongly_blocked": len(legit_blocked),
        "free_spam": spam_free,
        "semantic_spam": spam_semantic,
        "false_positives": legit_blocked,
    }


# ════════════════════════════════════════════════════════════════
# 二、門檻掃描
# ════════════════════════════════════════════════════════════════
class CachingJudge:
    """同一個 state 只判斷一次。

    ★ 為什麼需要：門檻掃描會拿同一批案例跑過每一個門檻（預設 7 個）✗
      沒有這層就是同一個問題問 7 次 ✗ 帳單也 7 倍。
      decide() 內部的呼叫也一併吃到紅利 ✗ 每個案例總共只花一次 API。

    ★ 而且它把失敗也記下來 ✗ 失敗會被記成例外物件 ✗ 重問時原樣拋出 ✗
      呼叫端因此能算出「到底有幾筆真的問到了」✗ 而不是把失敗當成「沒判斷」。
    """

    def __init__(self, inner):
        self.inner = inner
        self.cache: dict[str, object] = {}

    def judge(self, state, questions):
        key = json.dumps(state, sort_keys=True, ensure_ascii=False)
        if key not in self.cache:
            try:
                self.cache[key] = self.inner.judge(state, questions)
            except Exception as e:                      # noqa: BLE001
                self.cache[key] = e
        v = self.cache[key]
        if isinstance(v, Exception):
            raise v
        return v

    @property
    def ok(self) -> int:
        return sum(1 for v in self.cache.values() if not isinstance(v, Exception))

    @property
    def failed(self) -> int:
        return sum(1 for v in self.cache.values() if isinstance(v, Exception))

    def failures(self) -> list[str]:
        return sorted({type(v).__name__ + ": " + str(v)[:90]
                       for v in self.cache.values() if isinstance(v, Exception)})


def distribution(cases: list[dict], judge) -> dict:
    """把語意層真正判斷到的案例的信心值攤開。

    ★ 為什麼非有這一節不可：
      門檻掃描只能告訴你「哪個門檻結果最好」✗ 當所有門檻結果一樣時 ✗
      它只會說「沒有解析度」✗ 卻說不出為什麼。
      真正的原因通常是：**門檻落在模型的工作範圍之外** ✗
      模型對 spam 的信心最高就到 0.75 ✗ 你把 auto_block 設 0.90 ✗
      那個門檻就永遠不會觸發 ✗ 跟模型準不準無關。

      所以先把分佈印出來 ✗ 再談門檻要設哪裡。
    """
    rows = []
    for c in cases:
        if is_spam(c["text"]):
            continue                       # 規則層就攔下了 ✗ 沒問模型
        try:
            a = judge.judge(build_state(c["text"]), build_questions())
        except Exception as e:             # noqa: BLE001
            rows.append({"label": c["label"], "text": c["text"], "error": type(e).__name__})
            continue
        # ★ 用 decide.py 的正式讀取函式 ✗ 不要在工具裡自己假設欄位名。
        #   真 Jev 的 Noul 欄位叫 "noul" ✗ stub 的叫 "probability" ✗
        #   寫死任一邊都會在換 judge 時炸掉（已踩）。
        k, conf = _read_choice(a.get("kind") or {})
        probs = _read_probs(a.get("kind") or {})
        rows.append({
            "label": c["label"],
            "text": c["text"],
            "kind": k,
            "conf": conf,
            "needs": _read_noul(a.get("needs_human") or {}),
            "spam_p": probs.get("spam", 0.0) + probs.get("scam", 0.0),
        })
    return {"rows": rows}


def sweep(cases: list[dict], judge, gates: list[float], auto_allow: float = 0.70) -> list[dict]:
    rows = []
    for g in gates:
        th = Thresholds(auto_block=g, auto_allow=auto_allow, review=0.35).clamp()
        blocked_spam = missed_spam = blocked_legit = reviews = api_calls = 0
        for c in cases:
            # 規則層已攔下的不進 API ✗ 要算進去
            if is_spam(c["text"]):
                action = "block"
            else:
                # ★ 原本這裡先 judge() 一次「為了算 api_calls」✗ 然後 decide() 又呼叫一次 ✗
                #   等於每案問兩次 ✗ 失敗還被 except 吞掉 ✗ 掃描結果因此完全失真。
                #   現在只呼叫一次 ✗ 次數由快取自己算 ✗ 失敗有沒有發生也看得見。
                action = decide(c["text"], judge=judge, thresholds=th).action
                api_calls = judge.ok

            if c["label"] == "spam":
                if action == "block":
                    blocked_spam += 1
                else:
                    missed_spam += 1
            else:
                if action == "block":
                    blocked_legit += 1
                if action == "review":
                    reviews += 1
        rows.append({
            "gate": g, "blocked_spam": blocked_spam, "missed_spam": missed_spam,
            "blocked_legit": blocked_legit, "reviews": reviews, "api_calls": api_calls,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", choices=["stub", "typesafe"], default="stub")
    # ★ 預設門檻要涵蓋模型真正工作的區間 ✗ 實測 Jev 對垃圾訊息的 risk 落在 0.40-0.97 ✗
    #   全部設在 0.50 以上會看不到低端那一段 ✗ 掃描就失去意義。
    ap.add_argument("--gates", default="0.30,0.40,0.45,0.50,0.60,0.70,0.80,0.90")
    args = ap.parse_args()

    cases = load_cases()
    spam_n = sum(1 for c in cases if c["label"] == "spam")
    legit_n = len(cases) - spam_n

    print("═" * 78)
    print("  標註集：%d 案（spam %d ✗ legit %d）" % (len(cases), spam_n, legit_n))
    print("═" * 78)

    # ── 一、規則層 ──────────────────────────────────────────────
    r = analyse_rules(cases)
    print("\n【一】規則層自己吃掉多少（這一節的數字現在就是真的）\n")
    print("  spam 總數                %3d" % r["spam_total"])
    print("  ├─ 規則層擋下（免費）     %3d  = %5.1f%%" % (
        r["spam_caught_free"], r["spam_caught_free"] / r["spam_total"] * 100))
    print("  └─ 需要語意層（要花錢）   %3d  = %5.1f%%" % (
        r["spam_to_semantic"], r["spam_to_semantic"] / r["spam_total"] * 100))
    print()
    print("  legit 總數               %3d" % r["legit_total"])
    print("  └─ 被規則層誤封           %3d  %s" % (
        r["legit_wrongly_blocked"],
        "✓ 沒有誤封" if r["legit_wrongly_blocked"] == 0 else "✗ 有誤封，規則要修"))

    print("\n  規則層漏掉、必須靠語意的 spam：")
    for c in r["semantic_spam"]:
        print("    · %-42s %s" % (c["text"][:40], c["note"][:34]))

    if r["false_positives"]:
        print("\n  ✗ 規則層誤封的 legit（要優先修）：")
        for c in r["false_positives"]:
            print("    · %s" % c["text"])

    # ── 二、門檻掃描 ────────────────────────────────────────────
    if args.judge == "typesafe" and not os.getenv("TYPESAFE_API_KEY"):
        print("\n  ✗ --judge typesafe 但沒有 TYPESAFE_API_KEY ✗ 改用 stub\n")
        args.judge = "stub"

    judge = StubJudge() if args.judge == "stub" else None
    if judge is None:
        from teleshield.decide import JevJudge
        judge = JevJudge()

    judge = CachingJudge(judge)

    label = ("離線 stub ✗ 不是模型判斷 ✗ 下面的門檻不能拿去用"
             if args.judge == "stub" else "真實 System One（Jev）")

    # ── 一之二、語意層的信心分佈（門檻掃描看不出這件事）──
    print("\n" + "─" * 78)
    print("【二】語意層的信心分佈 ✗ 門檻要設在哪裡，先看這一節")
    print("─" * 78)
    dist = distribution(cases, judge)
    rows_d = dist["rows"]
    errs = [x for x in rows_d if "error" in x]
    if errs:
        print("\n  ✗ %d 個案例的 API 呼叫失敗 ✗ 下面的分佈不完整" % len(errs))
    okr = [x for x in rows_d if "error" not in x]
    if okr:
        sem_spam = [x for x in okr if x["label"] == "spam"]
        sem_legit = [x for x in okr if x["label"] == "legit"]
        print("\n  進去語意層的案例：%d 個（spam %d ✗ legit %d）" % (len(okr), len(sem_spam), len(sem_legit)))
        print()
        print("  %-32s %-14s %-6s %-6s %s" % ("訊息", "模型判定的性質", "信心", "需人工", "spam+scam機率"))
        print("  " + "-" * 74)
        # ★ 迴圈變數不要叫 r ✗ 後面的規則統計也用 r ✗ 會蓋掉（已踩）
        for row in sorted(okr, key=lambda x: -(x["spam_p"] or 0)):
            print("  %-32s %-14s %-6.2f %-6.2f %.2f   %s" % (
                (row["text"][:30] + "…") if len(row["text"]) > 31 else row["text"],
                row["kind"], row["conf"], row["needs"], row["spam_p"],
                "← 標準答案 " + row["label"]))
        confs = [x["conf"] for x in sem_spam]
        needs_all = [x["needs"] for x in okr]
        print()
        if confs:
            print("  spam 案的信心：最低 %.2f ✗ 最高 %.2f ✗ 中位 %.2f" % (
                min(confs), max(confs), sorted(confs)[len(confs)//2]))
            print("  ★ 這就是 auto_block 的天花板 ✗ 設得比最高值還高就永遠不會觸發")
        print("  needs_human：全體最低 %.2f ✗ 最高 %.2f" % (min(needs_all), max(needs_all)))
        print("  ★ review 門檻若低於全體最低值 ✗ 每一則都會進人工 ✗ 閘門等於沒有作用")

    print("\n" + "─" * 78)
    print("【三】門檻掃描 ✗ judge = %s" % label)
    print("─" * 78)
    gates = [float(x) for x in args.gates.split(",")]
    rows = sweep(cases, judge, gates)

    print("\n  %-6s %-8s %-8s %-10s %-8s %-8s" % ("門檻", "封到spam", "漏掉", "誤封legit", "review", "API呼叫"))
    print("  " + "-" * 62)
    for x in rows:
        flag = ""
        if x["blocked_legit"] > 0:
            flag = "  ✗ 誤封"
        elif x["missed_spam"] == 0:
            flag = "  ✓"
        print("  %-6.2f %-8d %-8d %-10d %-8d %-8d%s" % (
            x["gate"], x["blocked_spam"], x["missed_spam"],
            x["blocked_legit"], x["reviews"], x["api_calls"], flag))

    # ── 建議 ────────────────────────────────────────────────────
    print()
    # ★ 平台偵測：若多個門檻給出完全一樣的結果 ✗ 掃描就沒有解析度
    distinct = {(x["blocked_spam"], x["missed_spam"], x["blocked_legit"]) for x in rows}
    if len(distinct) <= 2:
        print("  ⚠ 掃描結果只有 %d 種 ✗ 門檻幾乎沒有解析度。" % len(distinct))
        if args.judge == "stub":
            print("    原因是 stub 的信心值是離散固定的（0.94 / 0.62–0.82 / 0.48–0.66 / 0.70）✗")
            print("    換成真實模型才會有連續分佈 ✗ 掃描才有意義")
        else:
            print("    ★ 平台期通常不是模型的問題 ✗ 是門檻落在模型的工作範圍之外 ✗")
            print("      看上面【二】的分佈：把 auto_block 設得比模型的最高信心還高 ✗")
            print("      或把 review 設得比模型最低的 needs_human 還低 ✗ 掃描就沒有解析度。")
        print()

    ok = [x for x in rows if x["blocked_legit"] == 0]
    if ok:
        # ★ 不要從平台期裡挑一個點當「建議」✗ 那是假精確 ✓
        #   平台期代表這個區間內所有門檻行為相同 ✗ 該報區間 ✓
        target = min(x["missed_spam"] for x in ok)
        band = [x["gate"] for x in ok if x["missed_spam"] == target]
        if len(band) == 1:
            print("  零誤封且漏封最少的是 auto_block = %.2f" % band[0])
        else:
            print("  零誤封且漏封最少的是 auto_block ∈ [%.2f, %.2f] ✗ 這一段是平台期" % (min(band), max(band)))
            print("  （區間內行為完全相同 ✗ 挑哪個都一樣 ✗ 取中間值沒有意義）")
        print("      漏封 %d ✗ 誤封 0 ✗ review %d 則" % (target, ok[0]["reviews"]))
        print()
        if args.judge == "stub":
            print("  ★ 但這是 stub 的結果 ✗ 只能證明工具會動")
        elif judge.failed:
            print("  ✗ 這次跑的不是真實結果 ✗ %d / %d 個案例的 API 呼叫失敗了"
                  % (judge.failed, judge.ok + judge.failed))
            print("    失敗的案例會退回規則層 ✗ 所以上面的「漏封」有一部分是假的 ✗")
            print("    修掉之後重跑 ✗ 這份掃描不能拿去定門檻 ✗")
            for f in judge.failures()[:5]:
                print("      · %s" % f)
        else:
            print("  ★ 這是真實模型的結果 ✗ %d / %d 個案例全部成功 ✗ 可以用"
                  % (judge.ok, judge.ok + judge.failed))
    else:
        print("  ✗ 所有門檻都有誤封 ✗ 代表語意層本身分不開這些案例")
        print("    先別開自動封鎖 ✗ 全部進人工 ✗ 並把誤封的案例加進標註集")

    print("\n  ★ 提醒：這份標註集只有 %d 案，其中 spam 多半是規則抓得到的" % len(cases))
    print("    真正該累積的是**規則漏掉、但語意抓到的**那種案例（上面那 %d 個）" % r["spam_to_semantic"])
    print("    每遇到一個誤判就加一條 ✗ 標註集從真實對話長出來才會準")
    return 0


if __name__ == "__main__":
    sys.exit(main())
