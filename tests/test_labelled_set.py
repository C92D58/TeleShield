"""標註集與校正工具的回歸測試。

標註集是門檻的唯一依據 ✗ 所以它本身要有守衛：
格式對不對、來源有沒有亂編、以及**規則層的表現有沒有變動**。

最後那一條最重要：如果有人改了 SPAM_PATTERNS ✗ 這裡會立刻告訴他
誤封或漏封變了多少 ✗ 而不是等上線才發現。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from teleshield.patterns import is_spam

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "labelled_cases.json"


@pytest.fixture(scope="module")
def cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


# ── 格式 ────────────────────────────────────────────────────────
def test_fixture_is_well_formed(cases):
    assert len(cases) >= 30, "標註集太小 ✗ 門檻會沒有意義"
    seen = set()
    for c in cases:
        assert set(c) >= {"text", "label", "layer", "note"}, c
        assert c["label"] in ("spam", "legit"), c
        assert c["layer"] in ("regex", "semantic", "either"), c
        assert c["note"].strip(), "每一條都要寫為什麼這樣標"
        assert c["text"] not in seen, "重複的案例：%r" % c["text"]
        seen.add(c["text"])


def test_both_classes_are_represented(cases):
    spam = sum(1 for c in cases if c["label"] == "spam")
    legit = len(cases) - spam
    assert spam >= 10 and legit >= 10, "兩類都要有足夠樣本才算得上標註集"
    assert 0.3 <= spam / len(cases) <= 0.7, "類別嚴重失衡 ✗ 掃出來的門檻會偏"


def test_provenance_is_recorded():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prov = data.get("_provenance", {})
    assert prov.get("source") and prov.get("from"), "要記來源 ✗ 否則沒人知道這些案例哪來的"


# ── 規則層的回歸守衛 ────────────────────────────────────────────
def test_rule_layer_never_blocks_legit(cases):
    """★ 誤封是最貴的錯 ✗ 這一條絕不允許退步。"""
    wrong = [c["text"] for c in cases if c["label"] == "legit" and is_spam(c["text"])]
    assert not wrong, "規則層誤封了正常訊息：%r" % wrong


def test_rule_layer_coverage_does_not_regress(cases):
    """規則層免費吃掉的比例 ✗ 掉了就代表有人把規則改弱了。"""
    spam = [c for c in cases if c["label"] == "spam"]
    free = sum(1 for c in spam if is_spam(c["text"]))
    ratio = free / len(spam)
    assert ratio >= 0.70, (
        "規則層只擋下 %.1f%% 的 spam（基準 76.2%%）✗ 語意層的工作量會暴增" % (ratio * 100)
    )


def test_semantic_band_is_not_empty(cases):
    """★ 若規則層已經全吃 ✗ 語意層就沒有存在的理由 ✗ 這份標註集就失去意義。"""
    spam = [c for c in cases if c["label"] == "spam"]
    need = [c["text"] for c in spam if not is_spam(c["text"])]
    assert need, "規則層全吃 ✗ 但語意層的存在理由應該要有一批『規則抓不到的廣告』"


# ── 校正工具能跑 ────────────────────────────────────────────────
def test_calibrate_runs_offline():
    r = subprocess.run([sys.executable, "tools/calibrate.py", "--judge", "stub"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    out = r.stdout
    assert "規則層自己吃掉多少" in out
    assert "門檻掃描" in out
    # 一定要講清楚 stub 不是模型判斷 ✗ 否則數字會被誤用
    assert "不是模型判斷" in out
