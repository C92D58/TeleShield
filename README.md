# TeleShield-UI

TeleShield 的原生 macOS SwiftUI fork。這個分支以單一 Swift 執行體承載 UI、Telegram 協議、資料層與背景任務。

上游原作與領域邏輯歸 [WAHSUN 的 TeleShield](https://github.com/c92d58/TeleShield) 及其作者所有；本 repository 延續 MIT License 與 attribution。

## 架構

```text
SwiftUI Views
    ↓
@MainActor CoreClient
    ↓
ProtectionCoordinator actor（common PTS + per-channel PTS、fail-closed）→ SpamRuleEngine
    ↓
TelegramAPI actor
    ↓
MTProtoClient actor
    ├── TLCodec
    ├── MTProtoCrypto（RSA、PQ/DH、AES-IGE、SRP）
    └── MTProtoTransport（Network.framework）
    ↓
Telegram MTProto

CoreClient → TeleShieldStore actor → JSON files + Keychain
```

主要模組位於 `swiftui/Sources/TeleShieldApp/`：

- `ContentView.swift`、`DashboardViews.swift`、`ModerationViews.swift`、`SettingsAndAccountViews.swift`：按 app shell、dashboard、moderation 與 settings/accounts 分層的 SwiftUI 畫面。
- `CoreClient.swift`：UI 狀態協調，不直接處理 binary protocol。
- `TeleShieldStore.swift`：帳號隔離、atomic persistence、Keychain credentials/session。
- `TelegramAPI.swift`、`TelegramOperations.swift`、`TelegramUpdates.swift`：登入、SRP、dialogs、history、common `updates.getDifference`、每個頻道／超級群組的 `updates.getChannelDifference`、封鎖與群組操作。
- `MTProtoClient.swift`、`MTProtoCrypto.swift`、`MTProtoTransport.swift`、`TLCodec.swift`：原生 MTProto stack。
- `ProtectionCoordinator.swift`、`SpamRuleEngine.swift`：即時防護、歷史掃描與規則判斷。

MTProto 實作依 Telegram 官方的 [MTProto 2.0 說明](https://core.telegram.org/mtproto/description)、[auth key exchange](https://core.telegram.org/mtproto/auth_key) 與 [TL schema](https://core.telegram.org/schema)；大整數運算使用純 Swift 的 [BigInt](https://github.com/attaswift/BigInt)。

## 功能

- Telegram 驗證碼登入與兩步驟密碼（SRP）
- 私訊封鎖、管理員群組成員移除
- 即時背景防護與歷史掃描；common account cursor 與每個 channel/supergroup PTS 分開持久化
- 白名單、黑名單、學習關鍵字與 regex 規則
- 封鎖記錄、報告、群組設定與 JSON／CSV 匯入／匯出
- 每個 Telegram 帳號獨立的 session、設定、名單與記錄
- SwiftUI dashboard、掃描進度、dry-run 與 destructive action confirmation

## 建置與測試

需求：macOS 13+、Swift 5.9+。建議使用完整 Xcode 以執行 XCTest；僅有 CommandLineTools 時可以執行 release build，但可能沒有 XCTest module。

```bash
swift package resolve --package-path swiftui
swift build --package-path swiftui -c release
swift test --package-path swiftui
git diff --check
```

若本機只有 CommandLineTools 且 `dsymutil` 無法正常完成，可在本機驗證時加上 `-Xswiftc -gnone`；CI 使用完整 macOS runner 執行標準建置與 XCTest。

建立本機 app bundle：

```bash
scripts/build_swiftui_macos.sh
```

CI 會在完整 Xcode 的 Intel `macos-15-intel` 與 Apple Silicon `macos-14` runner 各自執行 XCTest、Release build、架構／metadata 檢查。兩個 job 都只上傳 unsigned `.dmg`；下載的 artifact 解壓後只會有對應架構的 DMG。手動測試流程：

1. 在 GitHub 開啟 **Actions → SwiftUI desktop builds → Run workflow**，或 push 到 `codex/**` 分支觸發 workflow。
2. 等 `macOS-arm-swiftui` 與 `macOS-intel-swiftui` 兩個 job 完成。
3. 下載對應 artifact，解壓後開啟唯一的 `.dmg`；若測試失敗，請從 Actions job log 取得 XCTest 輸出。

這些檢查不取代實體 macOS 手動驗收或真實 Telegram 帳號測試。

登入／介面人工驗收建議：

1. 開啟對應架構的 `.dmg`，將 TeleShield 拖曳到 Applications；首次啟動時使用 macOS 的「打開」允許 unsigned app。
2. 填入自己的 Telegram API ID／API hash，確認驗證碼登入、2FA 密碼登入與重新啟動後 session 仍可用。
3. 先以 dry-run 掃描測試私訊，再用測試帳號送出明確的廣告規則文字，確認防護狀態、事件紀錄與封鎖記錄更新。
4. 若測試群組具備管理權限，再以測試成員驗證移除流程；不要使用管理員或擁有者作為測試對象。
5. 測試完成後回報 runner 架構、XCTest log、登入結果、UI 操作結果與防護結果；不要貼 API hash、驗證碼、2FA 密碼、auth key 或 session 內容。

## 本機資料

預設位置為：

```text
~/Library/Application Support/TeleShield
```

測試時可使用 disposable directory：

```bash
TELESHIELD_DATA_DIR="$PWD/.local-teleshield-data" swift run --package-path swiftui TeleShieldApp
```

API hash、電話與 auth key 儲存在 macOS Keychain；非敏感設定、common `updates.json`、channel `channel_updates.json` 與歷史記錄存於帳號隔離的 JSON 檔案。不要把 session、API credentials、驗證碼、2FA 密碼或真實 log 加入 Git。

## 安全注意事項

TeleShield 使用個人帳號 MTProto，不是 Bot API。請只在自己控制的帳號與群組使用，並先以 dry-run 檢查封鎖／移除結果。登出、刪除帳號資料、封鎖與群組移除都是 destructive operation。

本 fork 目前仍是測試版，已在原生層處理常見的 Telegram `*_MIGRATE_X` RPC、檔案 DC 回復路徑，以及以持久化 common `updates.getDifference` 與每個 channel/supergroup 的 `updates.getChannelDifference` 驅動背景同步；common `differenceTooLong` 與 channel `channelDifferenceTooLong` 都會安全重新建立各自 baseline，不會自動回溯處理無法確認的歷史缺口。尚未宣稱完成 Apple Developer signing、notarization、所有 Telegram DC／CDN failover、完整 Layer 223 TL constructor 覆蓋或真實 Telegram 帳號的端到端驗收。更新 response 遇到未支援 constructor 時會 fail-closed 並保留對應 cursor；OCR 支援可下載的照片訊息，不支援的媒體與 constructor 會安全跳過。
