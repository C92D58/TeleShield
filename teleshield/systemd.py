"""systemd 部署與 logrotate 設定產生。

把 `teleshield --listen` 變成一個受 systemd 管理的常駐服務：

- `render_unit()` / `render_logrotate()`：純字串模板，不碰硬碟、不呼叫 systemd，
  可以先用 --dry-run 看內容再決定要不要寫入。
- `install()`：寫入 unit 檔與 logrotate 設定（原子寫入 + 600 權限）。
- `uninstall()`：移除上述檔案（不存在就跳過）。
- `status()`：呼叫 `systemctl is-active / is-enabled` 查狀態，失敗一律優雅回傳。

使用者級 vs 系統級：
- 使用者級（`system=False`，預設）：unit 寫到 `~/.config/systemd/user/teleshield.service`，
  可用 `systemctl --user enable --now teleshield`，不需要 root。
- 系統級（`system=True`）：unit 寫到 `/etc/systemd/system/teleshield.service`、
  logrotate 寫到 `/etc/logrotate.d/teleshield`，需要 root（沒有權限直接 raise PermissionError）。

★ 為什麼不用 python-systemd 套件：本專案硬規則是不新增依賴，而我們只需要「產生設定檔 +
  呼叫 API 的 systemctl」兩件事，字串模板 + subprocess 就完全夠用；python-systemd 還需要
  libsystemd 的開發標頭，在精簡系統上常裝不起來。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import HOME_DIR

__all__ = [
    "HOME_DIR",
    "UNIT_NAME",
    "SYSTEMCTL_TIMEOUT",
    "RESTART_SEC",
    "START_LIMIT_INTERVAL_SEC",
    "START_LIMIT_BURST",
    "render_unit",
    "render_logrotate",
    "install",
    "uninstall",
    "status",
]

UNIT_NAME = "teleshield"
UNIT_FILE = f"{UNIT_NAME}.service"

# systemctl 查詢逾時（秒）。★ 為什麼一定要設逾時：查狀態是「順手看一下」，
# 但在極端環境（systemd 卡住、D-Bus 沒回應）systemctl 可能掛住不返回，
# 不設逾時會讓整個 CLI 陪著卡死。
SYSTEMCTL_TIMEOUT = 5

# ★ 為什麼 RestartSec 不能太小：服務啟動時要跑 Telethon 登入，失敗後若立刻重啟，
# 會連續打 Telegram API（FloodWait / 帳號風控），journal 也會被重啟訊息灌爆。
# 5 秒讓網路、DNS 與上游有喘息空間。
RESTART_SEC = 5

# ★ 為什麼要設 StartLimitIntervalSec / StartLimitBurst：systemd 預設是 10 秒內 5 次
# 就放棄（start-limit-hit）。對需要網路依賴的常駐服務來說，開機時網路還沒好就可能
# 用完 5 次，服務從此停在 failed 不再嘗試。放寬到 5 分鐘內 5 次，兼顧「快速自癒」與
# 「不要無聲無限重啟」——真的壞掉時仍會停下來等人介入。
START_LIMIT_INTERVAL_SEC = 300
START_LIMIT_BURST = 5

# 由 render_unit 自己負責的環境變數，extra_env 想覆蓋會被忽略
# （★ 避免同一個 unit 出現兩行 Environment=TELESHIELD_HOME，後者悄悄蓋掉前者）
_RESERVED_ENV = ("TELESHIELD_HOME",)

# 進程自己的日誌目錄名（logrotate 輪替 <home>/logs/*.log）
LOG_DIR_NAME = "logs"

# 服務被視為「正在跑」的 systemctl is-active 狀態
_ACTIVE_STATES = ("active", "activating", "reloading")

# systemctl 的雜訊（代表環境沒 systemd / 沒 bus，而不是服務狀態）
_FAILURE_NOISE = (
    "failed to connect to bus",
    "not been booted with systemd",
    "running in chroot",
)


def _quote(value: str) -> str:
    """systemd 設定值轉義。

    ★ 為什麼需要：unit 檔的值遇到空白會被截斷（ExecStart 會被拆成 argv，
      路徑有空白就找不到直譯器）；而 `%` 是 systemd 的 specifier 前綴
      （%i/%h/%n/%t），不跳脫會被展開成別的字串。
    """
    if value == "":
        return '""'
    needs_quotes = any(ch.isspace() for ch in value) or '"' in value or "'" in value or "%" in value
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' if needs_quotes else escaped


def _env_line(key: str, value: str) -> str:
    """組出一行 `Environment=KEY=VALUE`（含必要引號）。"""
    return f"Environment={_quote(f'{key}={value}')}"


def _default_exec_start() -> str:
    """預設 ExecStart：目前這個直譯器 + `-m teleshield --listen`。

    ★ 為什麼不寫死 "python3"：安裝時（venv / pipx）用的直譯器才是裝了 telethon 的那一個。
      寫死 python3 會指到系統直譯器，服務一啟動就 ImportError。用 sys.executable
      等於「用誰安裝就一直用誰跑」。
    """
    exe = sys.executable or shutil.which("python3") or "python3"
    return f"{_quote(exe)} -m {UNIT_NAME} --listen"


def _unit_path(system: bool) -> Path:
    """unit 檔位置（路徑在呼叫時才組，才吃得到 monkeypatch 過的 Path.home()）。"""
    if system:
        return Path("/etc/systemd/system") / UNIT_FILE
    return Path.home() / ".config" / "systemd" / "user" / UNIT_FILE


def _logrotate_path(system: bool, home: Path) -> Path:
    """logrotate 設定位置。

    ★ 為什麼使用者級不寫 ~/.config/logrotate.d/：logrotate 只讀 /etc/logrotate.conf 與
      /etc/logrotate.d/（守護進程以 root 執行），使用者級的 logrotate.d 根本不會被讀取，
      寫過去只會留下永遠不生效的死設定。使用者級改成放 <home>/logrotate.conf，
      需要時手動 `logrotate -s <home>/logrotate.state <home>/logrotate.conf` 執行。
    """
    if system:
        return Path("/etc/logrotate.d") / UNIT_NAME
    return home / "logrotate.conf"


def _atomic_write_text(path: Path, text: str) -> None:
    """原子寫入（暫存檔 + rename）並收緊權限為 600。

    ★ 為什麼要 600：unit 檔會揭露資料目錄、直譯器路徑與（透過 extra_env 傳入的）憑證路徑，
      logrotate 設定同理。預設 umask 022 會做出 644，同機其他使用者都讀得到。
    ★ 為什麼要暫存檔 + rename：直接寫入若中途失敗會留下半截 unit；rename 在同一檔案系統內
      是原子操作，systemd 只會看到「完整」或「不存在」兩種狀態。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def render_unit(
    *,
    home: Path = None,
    exec_start: str = None,
    user: str = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """產生 systemd unit 檔的文字內容（純函式，不寫檔、不呼叫 systemd）。

    :param home: 資料目錄，預設 `teleshield.config.HOME_DIR`（~/.teleshield）
    :param exec_start: 自訂 ExecStart；預設由 `sys.executable` 推導成
        `<python> -m teleshield --listen`（呼叫端自己給的值會原樣輸出，不轉義）
    :param user: 以哪個系統帳號執行（只在系統級安裝有意義，會產生 User=/Group= 兩行）
    :param extra_env: 追加的 Environment= 環境變數（TELESHIELD_HOME 會被忽略）

    產出的 unit 包含 Restart=always、RestartSec、StartLimit* 與安全強化
    （NoNewPrivileges / PrivateTmp / ProtectSystem / ProtectHome / ReadWritePaths）。
    """
    home_dir = HOME_DIR if home is None else Path(home)
    exec_line = exec_start or _default_exec_start()

    lines = [
        "# TeleShield systemd unit — 由安裝流程自動產生，手動修改會在下次安裝時被覆蓋。",
        "[Unit]",
        "Description=TeleShield — Telegram 廣告封鎖守衛（即時監聽）",
        # ★ 為什麼要 network-online：服務啟動就要連 Telegram，等網路就緒可少一次失敗重啟
        "After=network-online.target",
        "Wants=network-online.target",
        "# 崩潰循環保護（見 START_LIMIT_INTERVAL_SEC 的說明）",
        f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}",
        f"StartLimitBurst={START_LIMIT_BURST}",
        "",
        "[Service]",
        "Type=simple",
    ]

    if user:
        # ★ 為什麼系統級要指定 User=：常駐服務會讀寫 Telegram session（等同帳號控制權）、
        #   解析外部訊息，用 root 跑等於把整個系統一起押上去。
        lines += [f"User={_quote(user)}", f"Group={_quote(user)}"]

    lines += [
        f"ExecStart={exec_line}",
        # ★ 為什麼要 WorkingDirectory：目錄不存在時 systemd 會在執行前就以 200/CHDIR 失敗
        f"WorkingDirectory={_quote(str(home_dir))}",
        _env_line("TELESHIELD_HOME", str(home_dir)),
        # ★ 為什麼要 PYTHONUNBUFFERED：日誌經 journal 收集，Python 的區塊緩衝會讓
        #   `journalctl -f` 慢好幾 KB 才看到東西（純文字日誌檔同理）
        _env_line("PYTHONUNBUFFERED", "1"),
    ]

    for key, value in sorted((extra_env or {}).items()):
        if key in _RESERVED_ENV:
            continue
        lines.append(_env_line(str(key), str(value)))

    lines += [
        "",
        "# 重啟策略：一律重啟（斷網、FloodWait 之後能自己爬起來）",
        "Restart=always",
        f"RestartSec={RESTART_SEC}",
        "",
        "# 安全強化：這個進程只需要寫自己的資料目錄，其餘一律收掉",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=full",
        # ★ ProtectHome=read-only 與 ReadWritePaths 是一對：前者把整個 /home（含 /root）變唯讀，
        #   後者只把資料目錄重新開放寫入，其餘家目錄內容（SSH key、其他專案）一律碰不到
        "ProtectHome=read-only",
        f"ReadWritePaths={_quote(str(home_dir))}",
        "",
        "[Install]",
        # ★ default.target 在使用者級就是預設目標；系統級時它是 multi-user.target 的別名，
        #   所以同一份 unit 兩種層級都能 `systemctl enable`
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_logrotate(*, home: Path = None, keep_days: int = 14) -> str:
    """產生 logrotate 設定文字。

    輪替目標是 `<home>/logs/*.log`：常駐進程把日誌寫在那個目錄，目錄或檔案不存在也不要緊，
    `missingok` 會讓 logrotate 安靜跳過而不是報錯。

    :param home: 資料目錄，預設 `teleshield.config.HOME_DIR`
    :param keep_days: 保留份數（`rotate N`），非法值回退為 14

    ★ 為什麼一定要 copytruncate：常駐的 `--listen` 進程整個生命週期都開著日誌檔，
      logrotate 預設的 rename 輪替會把 *.log 搬走，但進程手上的 fd 仍指向已被搬走的舊 inode
      → 新的 .log 永遠是 0 byte，日誌等於消失（非得重啟服務才會好）。copytruncate 先複製再
      原地截斷，不換 fd、不必重啟。代價是複製與截斷之間寫入的幾行可能遺失，對日誌可以接受。
    """
    home_dir = HOME_DIR if home is None else Path(home)
    try:
        days = max(1, int(keep_days))
    except (TypeError, ValueError):
        days = 14
    log_glob = f"{home_dir}/{LOG_DIR_NAME}/*.log"

    lines = [
        "# TeleShield logrotate 設定 — 由安裝流程自動產生（目標：常駐 --listen 的日誌）。",
        "# ★ 為什麼用 copytruncate：常駐進程一直開著日誌檔，rename 輪替會讓新檔永遠是空的；",
        "#   copytruncate 先複製再原地截斷，不必重啟服務（可能丟掉複製與截斷之間的幾行）。",
        f"# 目標：{log_glob}（不存在時 missingok 會安靜跳過）",
        f"{log_glob} {{",
        "    daily",
        f"    rotate {days}",
        "    compress",
        # ★ delaycompress：壓縮延後一輪，避免剛輪替還在被寫入的那份檔立刻被壓
        "    delaycompress",
        "    missingok",
        "    notifempty",
        "    copytruncate",
        "}",
        "",
    ]
    return "\n".join(lines)


def install(
    *,
    system: bool = False,
    home: Path = None,
    user: str = None,
    dry_run: bool = False,
) -> dict:
    """安裝 unit 與 logrotate 設定，回傳路徑與產出的內容。

    :param system: True = 系統級（/etc/...，需要 root）；False = 使用者級（預設，不需要 root）
    :param home: 資料目錄，預設 `teleshield.config.HOME_DIR`
    :param user: 以哪個系統帳號執行（系統級建議指定，避免用 root 跑常駐服務）
    :param dry_run: True 時**完全不碰硬碟**（連目錄都不建），只回傳「如果安裝會寫什麼」

    回傳 dict：
      - `unit_path` / `logrotate_path`：Path
      - `unit` / `logrotate`：寫入的完整文字
      - `wrote`：實際寫入的 Path 清單（dry_run 時為空）
      - `note`：安裝位置與後續操作說明

    使用者級安裝的 logrotate 限制：logrotate 只讀 /etc/logrotate.conf 與 /etc/logrotate.d/，
    使用者級的 ~/.config/logrotate.d 不會被讀取，所以 `system=False` 時設定改寫到
    `<home>/logrotate.conf`，需搭配
    `logrotate -s <home>/logrotate.state <home>/logrotate.conf` 手動執行，
    或由 root 端 include 進 /etc/logrotate.d/。
    """
    home_dir = HOME_DIR if home is None else Path(home)
    unit_text = render_unit(home=home_dir, user=user)
    logrotate_text = render_logrotate(home=home_dir)
    unit_path = _unit_path(system)
    logrotate_path = _logrotate_path(system, home_dir)

    if system:
        note = (
            f"系統級安裝：unit → {unit_path}，logrotate → {logrotate_path}。"
            f"裝完請執行 `systemctl daemon-reload && systemctl enable --now {UNIT_NAME}`。"
        )
    else:
        note = (
            f"使用者級安裝：unit → {unit_path}，logrotate → {logrotate_path}。"
            "logrotate 只讀 /etc/logrotate.conf 與 /etc/logrotate.d/（以 root 執行），"
            "~/.config/logrotate.d 不會生效，所以設定改放資料目錄；"
            f"需要輪替時執行 `logrotate -s {home_dir}/logrotate.state {logrotate_path}`，"
            f"或由 root 端 include 進 /etc/logrotate.d/。"
        )

    if dry_run:
        return {
            "unit_path": unit_path,
            "logrotate_path": logrotate_path,
            "unit": unit_text,
            "logrotate": logrotate_text,
            "wrote": [],
            "note": note,
        }

    if system and getattr(os, "geteuid", lambda: 0)() != 0:
        # ★ 先自己檢查而不是等 OSError：訊息能直接告訴使用者「該用 sudo 或改用使用者級」，
        #   而不是一句 Permission denied 讓人猜
        raise PermissionError(
            f"系統級安裝需要 root 權限：{Path('/etc/systemd/system')} 與 {Path('/etc/logrotate.d')} "
            f"只有 root 能寫。請用 `sudo ...` 重跑，或改用使用者級安裝（system=False，"
            f"寫到 {_unit_path(False)}）。"
        )

    wrote: list[Path] = []
    try:
        if not home_dir.exists():
            # ★ 600 的目錄：TELESHIELD_HOME 內有 user.session（等同帳號控制權），
            #   新建時就給 700，別等出事再補
            home_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                os.chmod(home_dir, 0o700)
            except OSError:
                pass
        _atomic_write_text(unit_path, unit_text)
        wrote.append(unit_path)
        _atomic_write_text(logrotate_path, logrotate_text)
        wrote.append(logrotate_path)
    except OSError as exc:
        # ★ 回滾：留下「有 unit 沒 logrotate」的半殘安裝，比明確失敗更難排查
        for path in wrote:
            try:
                path.unlink()
            except OSError:
                pass
        raise PermissionError(
            f"安裝失敗：{exc}。請確認對 {unit_path} 與 {logrotate_path} 有寫入權限"
            "（系統級安裝需要 sudo）。"
        ) from exc

    return {
        "unit_path": unit_path,
        "logrotate_path": logrotate_path,
        "unit": unit_text,
        "logrotate": logrotate_text,
        "wrote": wrote,
        "note": note,
    }


def uninstall(*, system: bool = False, home: Path = None) -> list[Path]:
    """移除安裝的檔案（不存在的直接跳過），回傳實際刪掉的路徑。

    除了 unit 與 logrotate 設定，也會清掉 `systemctl enable` 留下的
    `<目標>.wants/teleshield.service` 符號連結。

    ★ 為什麼要清那個連結：enable 只是在家目錄（或 /etc）建立指向 unit 的軟連結，
      只刪 unit 檔會留下斷掉的連結，systemd 每次開機都會嘗試拉起不存在的服務並記錯誤。
    """
    home_dir = HOME_DIR if home is None else Path(home)
    unit_path = _unit_path(system)
    candidates = [
        unit_path,
        _logrotate_path(system, home_dir),
        unit_path.parent / "default.target.wants" / UNIT_FILE,
    ]

    removed: list[Path] = []
    for path in candidates:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)
    return removed


def _first_meaningful_line(stream: str | None) -> str | None:
    """從 systemctl 輸出取第一行有意義的狀態字。

    純環境雜訊（bus 連不上、不是 systemd 開機）回傳 None；「找不到 unit」正規化成 "not-found"。
    """
    for raw in (stream or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(noise in low for noise in _FAILURE_NOISE):
            # ★ 一定要先判這條：「Failed to connect to bus: No such file or directory」
            #   也含 "no such file"，若先判 not-found 會把「沒有 systemd」誤判成「沒這個服務」
            continue
        if "no such file" in low or "not-found" in low:
            return "not-found"
        return line
    return None


def _systemctl_query(system: bool, verb: str) -> str | None:
    """執行 `systemctl [--user] <verb> teleshield`，回傳第一行輸出；查不到回傳 None。

    ★ 為什麼逾時 + 吞掉所有例外：查狀態不該讓 CLI 卡住，也不該在沒有 systemd 的容器裡
      丟 traceback。systemctl 對未知 unit 會把訊息丟到 stderr（exit code 非 0），
      所以 stdout 沒有東西時要看 stderr。
    """
    exe = shutil.which("systemctl")
    if exe is None:
        return None
    argv = [exe]
    if not system:
        argv.append("--user")
    argv += [verb, UNIT_NAME]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for stream in (proc.stdout, proc.stderr):
        line = _first_meaningful_line(stream)
        if line:
            return line
    return None


def status(*, system: bool = False) -> dict:
    """查詢服務狀態，永不拋例外。

    回傳 {"installed", "active", "enabled", "detail"}：
      - `installed`：unit 檔在硬碟上，或 systemd 認得這個服務
      - `active` / `enabled`：`is-active` / `is-enabled` 的原始字串（查不到為 None）
      - `detail`：人類可讀說明（含查不到的原因）

    找不到 systemctl（例如容器/精簡鏡像）時 installed=False，並在 detail 說明原因。
    """
    unit_path = _unit_path(system)
    unit_exists = unit_path.exists()
    scope = "系統級" if system else "使用者級"

    if shutil.which("systemctl") is None:
        return {
            "installed": False,
            "active": None,
            "enabled": None,
            "detail": (
                f"找不到 systemctl：這個環境沒有 systemd（常見於容器或精簡鏡像），"
                f"無法查詢{scope}服務狀態。unit 檔預期位置：{unit_path}。"
            ),
        }

    active = _systemctl_query(system, "is-active")
    enabled = _systemctl_query(system, "is-enabled")

    if active is None and enabled is None:
        return {
            "installed": unit_exists,
            "active": None,
            "enabled": None,
            "detail": (
                "systemctl 存在但查不到結果：可能沒有以 PID 1 執行 systemd，"
                "或使用者級 systemd（systemd --user）沒有啟動。"
                f"unit 檔預期位置：{unit_path}。"
            ),
        }

    installed = unit_exists or (enabled is not None and enabled != "not-found") or active in _ACTIVE_STATES
    detail = (
        f"{scope}服務：unit 檔 {unit_path}（{'存在' if unit_exists else '不存在'}），"
        f"active={active or '未知'}、enabled={enabled or '未知'}。"
    )
    if installed and enabled in ("disabled", "not-found"):
        flag = "" if system else "--user "
        detail += f"尚未設定開機自啟，可執行 `systemctl {flag}enable --now {UNIT_NAME}`。"

    return {
        "installed": installed,
        "active": active,
        "enabled": enabled,
        "detail": detail,
    }
