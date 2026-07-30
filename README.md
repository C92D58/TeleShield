> ## 致上游原作者 / Credit to the upstream author
>
> 本專案是從 [WAHSUN 的 TeleShield](https://github.com/c92d58/TeleShield) fork 出來的延伸工作。感謝原作者建立 Telegram 個人帳號防護、私訊封鎖、群組管理、OCR、學習規則與報告等核心功能；這個 fork 不取代、不重新宣稱上游原作的著作權，也不代表上游作者背書本 fork 的改動。
>
> Original work, design, and domain logic belong to the upstream TeleShield project and its author. This repository adds downstream desktop integration and macOS SwiftUI work while preserving the upstream attribution and MIT license.

<div align="center">
  <h1>🛡️ TeleShield-UI</h1>
  <p><strong>TeleShield 的桌面 UI 與 macOS SwiftUI fork</strong><br>
  <em>A desktop UI fork of TeleShield with a native SwiftUI shell and a Python/Telethon sidecar.</em></p>
  <p>
    <a href="https://github.com/c92d58/TeleShield">上游原作</a> ·
    <a href="https://github.com/caryyu0306/TeleShield-UI">本 fork</a> ·
    <a href="LICENSE">MIT License</a>
  </p>
</div>

---

## 專案定位 / Project status

這個 repository 同時保留兩條桌面路徑：

1. **既有跨平台路徑**：`desktop_app.py` 使用 PySide6，保留原本的 Python/Telethon 工作流。
2. **macOS SwiftUI 路徑**：`swiftui/` 提供原生 SwiftUI 畫面，Telegram 業務邏輯仍由不含 PySide6 的 Python sidecar 執行。

本 fork 的目標是讓既有 TeleShield 核心可以被桌面 UI 使用，而不是把 Telethon 核心重寫成 Swift。Python CLI 與 PySide6 路徑仍應維持向後相容。

> **目前狀態：測試版，不是正式 release。** macOS DMG 由 GitHub Actions 產生，現階段為 unsigned artifact，未完成 Apple Developer code signing、notarization 或 stapling。Gatekeeper 顯示開發者無法驗證時，並不代表 ARM／Intel 架構錯誤。

### 尚未宣稱已完成的驗證

以下項目不能只因 Python tests、sidecar self-test 或 DMG 產生成功就視為完整驗證：

- 真實 Telegram network login、驗證碼、2FA 與 session 到期流程
- 實體 macOS 上的完整 SwiftUI 手動操作驗收
- 即時防護、歷史掃描、群組踢除與 OCR 的真實帳號整合
- 正式簽章、notarization、stapling 與公開發布

---

## 功能概覽 / Features

### 既有 TeleShield 核心

- 私訊廣告掃描與封鎖
- 管理員群組訊息掃描與成員移除
- 即時私訊／群組監聽
- 文字與本機 Tesseract OCR 廣告辨識
- 白名單、黑名單與學習規則
- 封鎖記錄與每日／每週報告
- 每個 Telegram 帳號獨立的 Session、設定、名單與記錄

### 桌面 UI parity

- Telegram 帳號建立、切換、重新登入、登出與本機 Session 清理
- 登入失敗會保留登入畫面供重試；登入成功後會關閉登入 sheet
- Dashboard、即時防護、歷史掃描與進度／取消操作
- 歷史掃描範圍、日期、對話數與訊息數設定
- 白／黑名單 JSON／CSV 匯入與匯出
- 學習規則、報告圖表、封鎖記錄與匯出
- 群組管理、OCR runtime 狀態、開機啟動與 background mode
- 安全的 destructive operation confirmation 與明確 `dry_run` 狀態

---

## 架構 / Architecture

```text
┌──────────────────────────────────────────────────────────┐
│ Cross-platform legacy desktop                            │
│ desktop_app.py (PySide6)                                 │
│        │                                                  │
│        └── existing Python/Telethon core                  │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│ macOS native desktop                                     │
│ TeleShield.app                                           │
│   SwiftUI UI                                              │
│        │ line-delimited JSON-RPC over stdin/stdout         │
│        ▼                                                  │
│   Contents/Helpers/TeleShieldCore                         │
│   PyInstaller Python sidecar (no PySide6)                 │
│        │                                                  │
│        ├── core_service.py                                │
│        └── teleshield.py / Telethon                       │
└──────────────────────────────────────────────────────────┘
```

重要邊界：

- SwiftUI 不直接 import Python，也不直接持有 Telethon client。
- `core_service.py` 只提供 stdio、line-delimited JSON-RPC 與背景工作生命週期。
- `requirements-sidecar.txt` 不包含 PySide6；PySide6 只屬於 legacy desktop 依賴。
- sidecar 會處理帳號、登入、listener、掃描、OCR、名單、報告、Session 與 shutdown。
- 帳號工作必須以明確 `account_id` 綁定，避免多帳號共用 Session 或資料目錄。
- 關閉 sidecar 時會取消背景工作並等待可等待的 auth／scan／listener worker 結束。

### Repository map

| 路徑 | 用途 |
|---|---|
| `teleshield.py` | 上游 Python／Telethon 核心與 CLI |
| `desktop_app.py` | legacy PySide6 desktop UI |
| `desktop_platform.py` | 跨平台資料目錄與開機啟動 adapter |
| `core_service.py` | 不含 PySide6 的 stdio JSON-RPC sidecar |
| `swiftui/Sources/TeleShieldApp/` | macOS SwiftUI app、typed model、sidecar client |
| `swiftui/Tests/` | SwiftUI 狀態邏輯回歸測試 |
| `scripts/build_swiftui_macos.sh` | macOS app + PyInstaller helper bundle script |
| `.github/workflows/desktop-build.yml` | Python checks、legacy DMG、SwiftUI Intel／ARM build |
| `tests/` | Python sidecar、parity、安全與 lifecycle tests |
| `requirements.txt` | Telethon core 依賴 |
| `requirements-desktop.txt` | PySide6 legacy desktop 與 OCR／packaging 依賴 |
| `requirements-sidecar.txt` | SwiftUI sidecar 與 OCR／PyInstaller 依賴，不含 PySide6 |

---

## 安全邊界與資料處理 / Security

TeleShield 會以使用者自己的 Telegram 個人帳號透過 MTProto 執行操作。這不是 Bot API；請只在自己控制的帳號與裝置上使用，並先理解封鎖、移除群組成員與 Session 清理的不可逆風險。

### Credentials 與 Session

- 不要把 API ID、API hash、電話、驗證碼、2FA 密碼、Session、token 或其他 credentials 寫入 README、issue、PR、測試 fixture、CI log 或聊天訊息。
- 不要把真實 credentials 放進 shell history、command example、Git repository 或截圖。
- 登入資料由本機 UI／sidecar 交給 Telethon；sidecar 對 RPC event、錯誤與 log 做 credential-like value redaction。
- Telethon Session 與每個帳號的設定／記錄會放在使用者資料目錄，而不是 App bundle；Session 等同 Telegram 身分憑證，必須限制檔案權限並妥善保管。
- 登出、清除 Session、刪除帳號資料是 destructive operation；執行前確認帳號與資料範圍。
- 本文件與 CI 輸出不包含任何真實 credential；若要回報問題，請以 `[REDACTED]` 取代敏感值。

### Destructive operation

- CLI 的 `--dry-run` 只做預覽，不封鎖或移除成員。
- 桌面歷史掃描預設使用 dry-run；要套用封鎖／移除必須明確確認。
- JSON-RPC 的 `dry_run` 會按布林語意解析；字串 `"false"` 不應被當成 `true`。
- 群組管理需要 Telegram 管理員權限；請先用 dry-run 檢查結果，再決定是否實際執行。
- 不要為了測試而使用 `sudo`、`spctl --master-disable`，或關閉全域 macOS Gatekeeper。

---

## CLI 快速開始 / CLI quick start

### Prerequisites

- Python 3.9 或更新版本（CI 使用 Python 3.11）
- 你的 Telegram API application credentials
- （選用）Tesseract OCR；桌面 CI 會另外 bundle 英文／簡體中文 runtime

### 建立環境與安裝

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果需要 legacy PySide6 desktop：

```bash
python -m pip install -r requirements-desktop.txt
```

如果只需要 SwiftUI sidecar 的 Python 依賴：

```bash
python -m pip install -r requirements-sidecar.txt
```

### 首次登入

不要把 credentials 放在 command line 或貼進 shell history；使用互動輸入：

```bash
python teleshield.py --setup
```

登入完成後，Telethon 會使用本機 Session。若要測試隔離資料目錄，可先設定：

```bash
export TELESHIELD_DATA_DIR="$PWD/.local-teleshield-data"
python teleshield.py --setup
```

`TELESHIELD_DATA_DIR` 只適合 disposable local test；不要把產生的 Session 或設定加入 Git。

### 常用 CLI 操作

```bash
# 先預覽私訊結果，不執行封鎖
python teleshield.py --dry-run

# 實際掃描並封鎖私訊（destructive）
python teleshield.py --scan

# 預覽群組處理結果
python teleshield.py --group-scan --dry-run

# 實際掃描管理員群組並移除成員（destructive）
python teleshield.py --group-scan

# 啟動即時監聽
python teleshield.py --listen

# 狀態、報告與學習
python teleshield.py --status
python teleshield.py --report
python teleshield.py --report week
python teleshield.py --learn "[REDACTED]"

# 白名單／黑名單
python teleshield.py --whitelist list
python teleshield.py --blacklist list
```

CLI 是上游相容路徑；請以 `teleshield.py` 的實際輸出與程式碼為準，不要把 README 範例當成真實 Telegram network 驗證結果。

---

## Legacy PySide6 desktop

安裝 `requirements-desktop.txt` 後可啟動既有跨平台 UI：

```bash
python desktop_app.py
```

它保留系統匣／背景防護、登入、歷史掃描、管理中心、群組、OCR、報告、名單與 Session 管理。Windows／Linux 的既有 PySide6 workflow 不應依賴 SwiftUI；SwiftUI sidecar 的改動也不應把 PySide6 加入 `requirements-sidecar.txt`。

---

## macOS SwiftUI 測試版

SwiftUI app 位於 `swiftui/`，由 SwiftUI 管理畫面與狀態，`TeleShieldCore` sidecar 管理 Telegram／Telethon 業務核心。登入 state 的重要行為如下：

```text
auth_succeeded + current account → close login sheet
                    │
auth_failed          └──────────────→ keep sheet open for retry
```

### 取得 GitHub Actions artifact

macOS build 應透過 GitHub Actions 執行，而不是把本機產出的 app 當成發布 artifact：

1. 在 GitHub repository 開啟 **Actions → Desktop test builds**。
2. 使用 `workflow_dispatch`，或 push 到 `main`／`feat/**` 分支後等待 workflow。
3. 確認 Python checks、SwiftUI state tests、sidecar self-test、OCR runtime 與目標架構檢查都成功。
4. 下載對應 artifact：

   ```text
   macOS-intel-swiftui
   macOS-arm-swiftui
   ```

Workflow 也會建立 legacy `macOS-intel` 與 `macOS-arm` artifact。SwiftUI app 的預期架構由 CI 以 `lipo -archs` 驗證：Intel 為 `x86_64`，Apple Silicon 為 `arm64`。

### unsigned DMG 的安全測試方式

下載後先驗證檔案來源與 checksum，再用 Finder 對已驗證的 `.app` 右鍵選 **打開 → 打開**。不要關閉全域 Gatekeeper。若需要診斷 quarantine，只對已驗證的單一 app 處理，不要使用 `sudo`：

```bash
shasum -a 256 ~/Downloads/macOS-arm-swiftui.dmg
xattr -l "/Applications/TeleShield.app"
open "/Applications/TeleShield.app"
```

目前 artifact 未簽章、未 notarize；這是測試限制，不是正式發行承諾。正式發布仍需要 Apple Developer signing、notarization 與 stapling credentials，這些 credentials 不存放在 repository 或 CI log。

---

## Sidecar JSON-RPC smoke test

`core_service.py` 提供兩種 headless 入口：

```bash
python core_service.py --self-test
```

或以 line-delimited JSON-RPC 測試無 credentials 的狀態、OCR 與 shutdown：

```bash
printf '%s\n' \
  '{"id":1,"method":"get_status"}' \
  '{"id":2,"method":"get_ocr_status"}' \
  '{"id":3,"method":"shutdown"}' \
  | python core_service.py --stdio
```

正常情況會收到相同 `id` 的 JSON response，並以 `ok: true` 表示該 request 成功。這只驗證 sidecar protocol 與本機 runtime，不等於真實 Telegram 登入或 network 操作成功。

---

## 測試與驗證 / Testing

### Python

```bash
python -m py_compile teleshield.py desktop_app.py desktop_platform.py core_service.py
python -m pytest -q
git diff --check
```

測試包含 sidecar protocol、帳號 lifecycle、名單／掃描／報告 parity、dry-run safety、redaction、listener unexpected exit 與 worker shutdown。Fake core／platform 測試不會連到真實 Telegram。

### SwiftUI

在 macOS 上：

```bash
swift test --package-path swiftui
swift build --package-path swiftui -c release
```

完整 app bundle 與 PyInstaller sidecar 的建置由：

```bash
scripts/build_swiftui_macos.sh
```

但本 repository 的雙架構 DMG、OCR bundle、helper self-test 與 artifact 產出以 `.github/workflows/desktop-build.yml` 為交付驗證來源。GitHub Actions 綠燈只代表 workflow 中列出的 checks 通過，不能取代實體 macOS 與真實 Telegram 帳號驗收。

---

## 本機資料目錄 / Local data

`TELESHIELD_DATA_DIR` 可覆寫預設資料根目錄；未設定時大致使用：

| 平台 | 預設位置 |
|---|---|
| macOS | `~/Library/Application Support/TeleShield` |
| Windows | `%APPDATA%/TeleShield` |
| Linux | `~/.local/share/TeleShield` |

帳號資料會分開保存 Session、設定、學習規則與封鎖記錄。實際檔名與 migration 以 `teleshield.py` 為準。這些資料不應加入 Git、上傳 issue，或放進 App bundle。

---

## 貢獻與 fork 原則 / Contributions

- 先閱讀上游 TeleShield 的設計與 MIT license，保留原作者 attribution。
- Python core 的修正應盡量維持 CLI 與 PySide6 backward compatibility。
- SwiftUI 只負責 native UI；Telegram／Telethon 邏輯應留在 sidecar。
- 新增功能時補上 headless regression test；不要用 credentials 來做 CI 測試。
- destructive operation 必須有明確 dry-run／confirmation；不要把預設值改成無提示執行。
- 不要提交 Session、API hash、電話、驗證碼、2FA password、token、真實 log 或任何 credential。
- 提交 macOS UI 變更前，使用 GitHub Actions 驗證 Swift 編譯與目標架構；CI artifact 是 unsigned test build，除非另行完成正式簽章流程。

---

## License

本 fork 延續上游 repository 的 [MIT License](LICENSE)。上游原作與作者 attribution 請保留；本 fork 新增的桌面整合與 macOS SwiftUI 變更同樣依 repository license 發布。

<div align="center">
  <sub>Respect the upstream author. Build downstream improvements carefully. 🛡️</sub>
</div>
