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
  --learn <文字>            手動標記學習新模式
  --whitelist add|remove|list [id]
  --blacklist add|remove|list [id]

環境變數：
  TELESHIELD_HOME           數據目錄（默認 ~/.teleshield）
  TELESHIELD_API_ID         --setup 用 API ID
  TELESHIELD_API_HASH       --setup 用 API Hash
  TELESHIELD_PHONE          --setup 用手機號
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
    elif cmd == "--learn":
        text = " ".join(argv[1:]) if len(argv) > 1 else ""
        if not text:
            print("❌ 請提供廣告文字，例如: --learn 加我微信 xxx 投資穩賺")
            return 1
        await learn(text)
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


def entry() -> None:
    """console_scripts 入口（同步包裝）。"""
    import asyncio

    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
