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
                try:
                    judge.judge(build_state(c["text"]), build_questions())
                    api_calls += 1
                except Exception:
                    api_calls += 1
                action = decide(c["text"], judge=judge, thresholds=th).action

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
    ap.add_argument("--gates", default="0.50,0.60,0.70,0.80,0.85,0.90,0.95")
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

    label = ("離線 stub ✗ 不是模型判斷 ✗ 下面的門檻不能拿去用"
             if args.judge == "stub" else "真實 System One（Jev）")

    print("\n" + "─" * 78)
    print("【二】門檻掃描 ✗ judge = %s" % label)
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
        print("    原因是 %s" % ("stub 的信心值是離散固定的（0.94 / 0.62–0.82 / 0.48–0.66 / 0.70）✗ "
                                 "換成真實模型才會有連續分佈 ✗ 掃描才有意義"
                                 if args.judge == "stub" else "模型對這批案例的信心過於集中"))
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
        print("  ★ 但這是 %s" % ("stub 的結果 ✗ 只能證明工具會動" if args.judge == "stub"
                                 else "真實模型的結果 ✗ 可以用"))
    else:
        print("  ✗ 所有門檻都有誤封 ✗ 代表語意層本身分不開這些案例")
        print("    先別開自動封鎖 ✗ 全部進人工 ✗ 並把誤封的案例加進標註集")

    print("\n  ★ 提醒：這份標註集只有 %d 案，其中 spam 多半是規則抓得到的" % len(cases))
    print("    真正該累積的是**規則漏掉、但語意抓到的**那種案例（上面那 %d 個）" % r["spam_to_semantic"])
    print("    每遇到一個誤判就加一條 ✗ 標註集從真實對話長出來才會準")
    return 0


if __name__ == "__main__":
    sys.exit(main())
