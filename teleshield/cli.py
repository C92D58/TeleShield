"""TeleShield CLI 入口：參數解析與命令分派。"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from .commands import (
    learn,
    listen,
    manage_list,
    report,
    scan_and_block,
    scan_groups,
    setup,
)
from .config import load_block_log, load_config

__all__ = ["main", "USAGE"]


USAGE = """TeleShield — Telegram 廣告封鎖工具
─────────────────────────────────
  --setup                   首次設定（憑證用環境變數或交互輸入，勿用參數）
  --scan                    掃描並封鎖私訊
  --dry-run                 試掃描
  --listen                  即時監聽（後台常駐）
  --group-scan              掃描群組並踢除廣告
  --status                  查看狀態
  --report [day|week]       封鎖摘要報告
  --report-html [day|week]  生成 HTML 可視化報告
  --learn <文字>            手動標記學習新模式
  --whitelist add|remove|list|import|export [id|file]
  --blacklist add|remove|list|import|export [id|file]

  --systemd install|uninstall|status [--system]
                             一鍵部署成常駐服務（--system 需 root）
  --update [--check]         檢查／安裝新版本（會驗 sha256）
  --ml train|stats           訓練／查看本機垃圾分類器
  --dashboard [--port N] [--allow-remote]
                             本機網頁儀表板（預設只綁 127.0.0.1）
  --cloud [status|pull|push|sync] [whitelist|blacklist|all] [--merge]
                             名單同步到 Cloudflare KV（可選 ✗ 需環境變數）

環境變數：
  TELESHIELD_HOME           數據目錄（默認 ~/.teleshield）
  TELESHIELD_API_ID         --setup 用 API ID
  TELESHIELD_API_HASH       --setup 用 API Hash
  TELESHIELD_PHONE          --setup 用手機號
  TELESHIELD_GITHUB_TOKEN   --update 用（私人倉庫需要）
  TELESHIELD_DASHBOARD_HOST / _PORT         儀表板綁定位址
  TELESHIELD_CF_ACCOUNT_ID / _CF_KV_NAMESPACE_ID / _CF_API_TOKEN
                            --cloud 用（三個都要填才會啟用）
"""


async def main(argv: list[str] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if not argv:
        print(USAGE)
        return 0

    cmd = argv[0]

    if cmd == "--setup":
        from .config import load_dotenv
        load_dotenv()
        await setup(
            os.getenv("TELESHIELD_API_ID"),
            os.getenv("TELESHIELD_API_HASH"),
            os.getenv("TELESHIELD_PHONE"),
            None,
        )
    elif cmd == "--scan":
        await scan_and_block(dry_run=False)
    elif cmd == "--dry-run":
        await scan_and_block(dry_run=True)
    elif cmd == "--group-scan":
        await scan_groups(dry_run="--dry" in argv or "dry" in argv)
    elif cmd == "--listen":
        await listen()
    elif cmd == "--status":
        cfg = load_config()
        if not cfg:
            print("❌ 尚未設定")
            return 0
        log = load_block_log()
        recent = len([b for b in log.get("blocks", []) if datetime.fromisoformat(b["time"]) > datetime.now(timezone.utc) - timedelta(days=1)])
        print("📊 TeleShield 狀態")
        print(f"{'─'*30}")
        print(f"  帳號: {cfg.get('username','?')} (ID: {cfg.get('user_id','?')})")
        print(f"  累計封鎖私訊: {cfg.get('blocked_count',0)} 人")
        print(f"  累計踢除群組: {cfg.get('kicked_count',0)} 人")
        print(f"  今日封鎖: {recent} 人")
        print(f"  白名單: {len(cfg.get('whitelist',{}))} 人")
        print(f"  黑名單: {len(cfg.get('blacklist',{}))} 人")
        print(f"  學習模式: {len(cfg.get('learned_patterns',{}).get('keywords',[]))} 關鍵詞")
        print(f"  最後掃描: {cfg.get('last_scan','從未')}")
    elif cmd == "--report":
        period = argv[1] if len(argv) > 1 else "day"
        await report(period)
    elif cmd == "--report-html":
        period = argv[1] if len(argv) > 1 else "week"
        await report(period, output_html=True)
    elif cmd == "--learn":
        text = " ".join(argv[1:]) if len(argv) > 1 else ""
        if not text:
            print("❌ 請提供廣告文字，例如: --learn 加我微信 xxx 投資穩賺")
            return 1
        await learn(text)
    elif cmd == "--systemd":
        return _cmd_systemd(argv)
    elif cmd == "--update":
        return _cmd_update(argv)
    elif cmd == "--ml":
        return _cmd_ml(argv)
    elif cmd == "--dashboard":
        return _cmd_dashboard(argv)
    elif cmd == "--cloud":
        return _cmd_cloud(argv)
    elif cmd in ("--whitelist", "--blacklist"):
        list_type = cmd.replace("--", "")
        action = argv[1] if len(argv) > 1 else "list"
        user_id = argv[2] if len(argv) > 2 else None
        await manage_list(action, list_type, user_id)
    else:
        print(f"❌ 未知指令: {cmd}")
        print("執行不加參數查看全部指令")
        return 1
    return 0


# ════════════════════════════════════════════════════════════════════
# 五個新指令
# ★ 為什麼放 cli.py 而不是 commands.py：commands.py 是「要連 Telegram」的
#   業務邏輯（掃描／監聽／名單）✗ 這五個都不需要 client ✗
#   放這裡就不必把使用者級服務、HTTP、雲端那些東西混進業務層。
# ════════════════════════════════════════════════════════════════════


def _cmd_systemd(argv: list[str]) -> int:
    """systemd 一鍵部署。"""
    from . import systemd

    action = argv[1] if len(argv) > 1 else "status"
    as_system = "--system" in argv
    flag = "" if as_system else "--user "

    if action == "install":
        try:
            r = systemd.install(system=as_system)
        except PermissionError as e:
            print(f"❌ {e}")
            return 1
        print("\n" + "═" * 40)
        print("  TeleShield - 安裝常駐服務")
        print("═" * 40 + "\n")
        for f in r.get("wrote", []):
            print(f"  ✅ 已寫入 {f}")
        print(f"  模式: {'系統級（開機即啟動）' if as_system else '使用者級（登入後啟動）'}")
        if r.get("note"):
            print(f"  ⚠  {r['note']}")
        print("\n  啟動：")
        print(f"    systemctl {flag}daemon-reload")
        print(f"    systemctl {flag}enable --now teleshield")
        print("\n  看日誌：")
        print(f"    journalctl {flag}-u teleshield -f")
    elif action == "uninstall":
        removed = systemd.uninstall(system=as_system)
        if removed:
            for f in removed:
                print(f"  ✅ 已移除 {f}")
        else:
            print("  （沒有安裝過的痕跡）")
        print("\n  別忘了停掉服務：")
        print(f"    systemctl {flag}disable --now teleshield")
    elif action == "status":
        st = systemd.status(system=as_system)
        print("\n  TeleShield 服務狀態")
        print(f"  {'─' * 30}")
        print(f"  已安裝  : {'是' if st.get('installed') else '否'}")
        print(f"  運行中  : {st.get('active') or '-'}")
        print(f"  開機啟用: {st.get('enabled') or '-'}")
        if st.get("detail"):
            print(f"  說明    : {st['detail']}")
    else:
        print(f"❌ --systemd 只接受 install / uninstall / status（收到 {action!r}）")
        return 1
    return 0


def _cmd_update(argv: list[str]) -> int:
    """檢查並安裝新版本。"""
    from . import updater

    check_only = "--check" in argv
    try:
        info = updater.check()
    except updater.UpdateError as e:
        print(f"❌ 檢查更新失敗：{e}")
        return 1

    print("\n" + "═" * 40)
    print("  TeleShield - 更新")
    print("═" * 40 + "\n")
    print(f"  目前版本: {info['current']}")
    print(f"  最新版本: {info['latest']}")

    if not info["update_available"]:
        print("\n  ✅ 已是最新版本")
        return 0

    if info.get("notes"):
        print("\n  ── 更新說明 ──")
        for line in str(info["notes"]).splitlines()[:20]:
            print(f"  {line}")

    if check_only:
        print("\n  （--check：只檢查 ✗ 沒有安裝）")
        return 0

    asset = updater.find_asset(info["release"])
    if not asset:
        print("\n  ❌ 這個 release 沒有可安裝的檔案（找不到 .whl / .tar.gz）")
        return 1

    digest = asset.get("digest") or asset.get("sha256")
    dest = updater.HOME_DIR / asset["name"]
    try:
        print(f"\n  下載 {asset['name']} …")
        updater.download(asset["browser_download_url"], dest, expected_sha256=digest)
        print("  ✅ 下載完成" + ("（sha256 已驗證）" if digest else "（此 release 未提供 checksum）"))
        r = updater.apply(dest)
        print(f"  ✅ 安裝完成（exit {r.get('returncode', 0)}）")
    except updater.UpdateError as e:
        print(f"  ❌ 更新失敗：{e}")
        return 1
    print("\n  重啟服務以套用新版本")
    return 0


def _cmd_ml(argv: list[str]) -> int:
    """本機分類器。"""
    from . import ml

    action = argv[1] if len(argv) > 1 else "stats"
    if action == "train":
        try:
            samples = ml.training_samples()
            r = ml.train_model(samples)
        except ValueError as e:
            print(f"❌ 無法訓練：{e}")
            print("  （只有一類資料訓練出來的模型會退化成永遠說 spam ✗ 所以拒絕訓練）")
            return 1
        print("\n  ✅ 模型已訓練")
        print(f"  {'─' * 30}")
        print(f"  樣本: {r['n_samples']}（spam {r['n_spam']} / legit {r['n_legit']}）")
        print(f"  特徵: {r['vocab']}")
        print(f"  訓練集準確率: {r['accuracy']:.1%}")
        print(f"  存放: {r['path']}")
        print("\n  ★ 那是訓練集準確率 ✗ 不是泛化能力 ✗ 別當成績看")
    elif action == "stats":
        model = ml.load_model()
        if model is None:
            print("  尚未訓練（這一層目前是關的 ✗ 判斷完全走規則 + 語意層）")
            print("  建立模型：teleshield --ml train")
            return 0
        print("\n  本機分類器")
        print(f"  {'─' * 30}")
        print(f"  樣本: {model.n_samples}")
        print(f"  類別: {', '.join(model.classes)}")
        print(f"  特徵: {model.vocab_size}")
        print(f"  位置: {ml.DEFAULT_MODEL_FILE}")
    else:
        print(f"❌ --ml 只接受 train / stats（收到 {action!r}）")
        return 1
    return 0


def _cmd_dashboard(argv: list[str]) -> int:
    """本機網頁儀表板。"""
    from . import dashboard

    host = os.getenv("TELESHIELD_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("TELESHIELD_DASHBOARD_PORT", "8787"))
    if "--allow-remote" in argv:
        host = "0.0.0.0"
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                print(f"❌ --port 需要一個數字（收到 {argv[i + 1]!r}）")
                return 1
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠  綁定非本機位址 ✗ 這個介面沒有密碼保護 ✗ 請自行確保網路可信")
    try:
        dashboard.serve(host=host, port=port)
    except OSError as e:
        print(f"❌ 無法啟動（{e}）✗ 埠可能被佔用")
        return 1
    return 0


def _cmd_cloud(argv: list[str]) -> int:
    """名單雲端同步。"""
    from . import cloudsync

    action, list_type = "sync", "all"
    for a in argv[1:]:
        if a in ("status", "pull", "push", "sync"):
            action = a
        elif a in ("whitelist", "blacklist", "all"):
            list_type = a

    try:
        if action == "status":
            st = cloudsync.CloudListSync().status()
            print("\n  雲端名單同步")
            print(f"  {'─' * 30}")
            print(f"  已設定: {'是' if st.get('configured') else '否'}")
            if not st.get("configured"):
                print("  需要 TELESHIELD_CF_ACCOUNT_ID / _CF_KV_NAMESPACE_ID / _CF_API_TOKEN")
                return 0
            for k in ("local", "remote"):
                d = st.get(k) or {}
                print(f"  {k}: " + ", ".join(f"{a}={b}" for a, b in d.items()))
            return 0
        results = cloudsync.cloud_sync(list_type, action=action, merge="--merge" in argv)
        for r in results:
            print(f"  ✅ {r.list_type}: 上傳 {r.pushed} / 下載 {r.pulled} / "
                  f"新增 {r.added} / 移除 {r.removed}")
            if r.detail:
                print(f"     {r.detail}")
    except cloudsync.CloudError as e:
        print(f"❌ {e}")
        return 1
    return 0


def entry() -> None:
    """console_scripts 入口（同步包裝）。"""
    import asyncio

    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
