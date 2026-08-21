<div align="center">
  <h1>🛡️ TeleShield</h1>
  <p><strong>Telegram 全能廣告封鎖守衛</strong><br>
  <em>Your personal Telegram spam firewall — private messages & group management, all in one.</em></p>

  <p>
    <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <img src="https://img.shields.io/badge/telethon-1.44%2B-purple" alt="Telethon">
    <img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.github.com%2Frepos%2FC92D58%2FTeleShield%2Freleases%2Flatest&query=%24.tag_name&label=release&color=22C55E" alt="Release">
  </p>
  <p>
    <a href="https://teleshield.wahsun.org">🌐 產品頁面</a>
    ·
    <a href="https://github.com/c92d58/TeleShield/releases/latest">📦 下載</a>
    ·
    <a href="https://github.com/c92d58/TeleShield#-快速開始--quick-start">🚀 快速開始</a>
  </p>
</div>

---

## 📋 概述 / Overview

**TeleShield** 是一個全功能的 Telegram 廣告防禦工具，涵蓋 **個人私訊封鎖** 與 **群組踢除** 兩大場景。不同於 Bot API，它直接以你的身份登入，能處理 Bot 做不到的個人帳號防護。

*TeleShield is a full-featured Telegram spam defense system covering **private DM blocking** and **group moderation**. It logs in as you — something Bot API bots cannot do.*

---

## ✨ 功能 / Features

| 功能 | 命令 | 說明 |
|------|------|------|
| 🔍 **私訊掃描** | `--scan` | 掃描近期非聯絡人對話，比對廣告模式並封鎖 |
| 👥 **群組掃描** | `--group-scan` | 掃描群組近期訊息，踢除發廣告的成員（需管理員權限） |
| 🛡️ **即時監聽** | `--listen` | 後台常駐，**同時監控私訊+群組**，秒級響應 |
| 🧠 **評分引擎** | 內建 | 多維垃圾訊號評分（分級正則/連結密度/@提及/弱帳號特徵/頻率），自動區分 封鎖/標記/放行 |
| 🔍 **群組行為分析** | 內建 | 新成員進群秒發連結、刷屏廣告 → 自動踢除 |
| 🧪 **試運行** | `--dry-run` | 安全預覽，只顯示結果不實際封鎖/踢除 |
| 📸 **圖片 OCR** | 內建 | 純圖片廣告 → Tesseract 本地辨識文字 → 模式比對，**資料不外傳** |
| 🧠 **學習模式** | `--learn <文字>` | 手動標記廣告，自動提取關鍵詞+生成正則模式 |
| 📊 **封鎖報告** | `--report [day\|week]`、`--report-html` | 每日/每週摘要 + **HTML 可視化報告**（類別統計/趨勢/明細） |
| ⚫ **黑名單** | `--blacklist add\|remove\|list\|import\|export [id\|file]` | 加入黑名單後自動封鎖/踢除；**JSON 交換**共享 community 名單 |
| ⚪ **白名單** | `--whitelist add\|remove\|list\|import\|export [id\|file]` | 白名單用戶永不被掃描、封鎖或踢除 |
| 📊 **狀態面板** | `--status` | 一覽封鎖數、踢除數、名單和學習模式狀態 |

---

## 🚀 快速開始 / Quick Start

### 前置需求 / Prerequisites

- Python 3.9+
- Telegram API 憑證（[my.telegram.org/apps](https://my.telegram.org/apps)）
- （選用）Tesseract OCR 用於圖片廣告辨識

### 安裝 / Install

**方式一：一鍵安裝（推薦）**

```bash
git clone https://github.com/c92d58/TeleShield.git
cd TeleShield
bash install.sh          # 檢查依賴 + pip 安裝 + 建立數據目錄
```

**方式二：pip 直接安裝**

```bash
pip install "teleshield[ocr]"
# 圖片 OCR 系統依賴（選用，強烈建議）
apt install tesseract-ocr tesseract-ocr-chi-sim
```

### 首次設定 / First-time Setup

```bash
teleshield --setup
```

依序輸入：
1. `API ID` — 從 [my.telegram.org/apps](https://my.telegram.org/apps) 取得
2. `API Hash` — 同上
3. `手機號碼` — 含國碼，如 `+852****5931`
4. `驗證碼` — Telegram 會發送驗證碼到你手機

> **安全提示**：憑證也可寫入 `~/.teleshield/.env`（`TELESHIELD_API_ID` / `TELESHIELD_API_HASH` / `TELESHIELD_PHONE`），`--setup` 會自動讀取。**切勿**用命令行參數傳憑證（會洩漏到 shell history）。

登入成功後自動儲存 Session（`~/.teleshield/`），下次不需重複登入。

### 基本用法 / Usage

```bash
# ─── 私訊防護 ───

# 先試運行看看結果
teleshield --dry-run

# 實際掃描近期待處理的廣告
teleshield --scan

# 啟動即時監聽（後台常駐，私訊+群組全保護）
teleshield --listen

# ─── 群組管理 ───

# 掃描所有管理中的群組，踢除廣告發送者
teleshield --group-scan

# ─── 學習與報告 ───

# 手動標記廣告文字，讓程式學習新模式
teleshield --learn "加微信 abc123 投資穩賺日入過萬"

# 查看封鎖摘要
teleshield --report         # 過去 24 小時
teleshield --report week    # 過去 7 天 + 趨勢
teleshield --report-html    # 生成 HTML 可視化報告（~/.teleshield/report_week.html）

# ─── 名單管理 ───

# 白名單（永不封鎖）
teleshield --whitelist add 12345678
teleshield --whitelist list

# 黑名單（見一個封一個）
teleshield --blacklist add 87654321

# community 名單交換（JSON 格式）
teleshield --blacklist export my_list.json
teleshield --blacklist import community.json
teleshield --blacklist remove 87654321

# 查看完整狀態
teleshield --status
```

---

## 📖 完整命令參考 / Full Command Reference

| 命令 | 說明 |
|------|------|
| `--setup` | 首次設定（憑證用**環境變數** `TELESHIELD_API_ID`/`TELESHIELD_API_HASH`/`TELESHIELD_PHONE` 或交互輸入，**勿用參數**） |
| `--scan` | 掃描非聯絡人私訊，封鎖廣告 |
| `--dry-run` | 試運行掃描（不實際封鎖） |
| `--listen` | **即時監聽模式** — 私訊封鎖 + 群組踢除 + 行為分析同時運作 |
| `--group-scan` | 掃描管理中的群組，踢除廣告發送者 |
| `--status` | 查看完整狀態面板 |
| `--report [day\|week]` | 封鎖摘要報告（預設 day） |
| `--report-html [day\|week]` | 生成 HTML 可視化報告（`~/.teleshield/report_<period>.html`） |
| `--learn <文字>` | 手動標記廣告文字，自動學習新模式 |
| `--whitelist add\|remove\|list\|import\|export [user_id\|file]` | 白名單管理（import/export 為 JSON 交換） |
| `--blacklist add\|remove\|list\|import\|export [user_id\|file]` | 黑名單管理（import/export 為 JSON 交換） |

---

## 👥 群組管理詳解

TeleShield 支援自動管理你具有**管理員權限**的群組：

| 場景 | 行為 |
|------|------|
| `--listen` 運行中 | 群組內有新訊息 → 自動檢測 → 踢除廣告發送者 |
| `--group-scan` | 掃描最近 20 條訊息 → 批次踢除 |
| 管理員自動跳過 | 群組管理員和創建者不受影響 |
| 白名單跳過 | 白名單中的用戶不會被踢除 |
| 3 天窗口 | 只檢查最近 3 天內的訊息 |

踢除使用 **ChatBannedRights(view_messages=True)**，相當於 Telegram 的「封鎖用戶 + 移除」，對方無法再次加入。

---

## 🧠 學習模式詳解

遇到新模式廣告時，使用 `--learn` 讓 TeleShield 自動學習：

```bash
# 範例：標記一個包含 URL 的廣告
teleshield --learn "https://bit.ly/3XabcDe 免費領取 BTC"

# 範例：標記一個 LINE/微信推廣
teleshield --learn "➕官方LINE：@free888 每日推薦飆股"
```

學習機制：

| 步驟 | 說明 |
|------|------|
| 🔍 提取關鍵詞 | 過濾停用詞，提取 2-6 字高價值關鍵詞 |
| 🧩 生成正則 | 自動從 URL、ID 等結構生成可複用的模式 |
| 💾 持久儲存 | 保存在 `config.json` 中，每次啟動載入 |
| 🔄 即時生效 | 學習後 `is_spam()` 立即使用新模式 |

累計學習結果可透過 `--status` 查看。

---

## 🧠 垃圾訊號評分引擎（v0.10.0）

取代單層正則判定——多維信號加權評分，自動區分 **封鎖 / 標記 / 放行**：

| 信號 | 加分 |
|------|------|
| 分級正則命中（高危 severe / 中危 moderate / 低危 low） | +3 / +2 / +1（語義簇獨立疊加） |
| 學習模式（關鍵詞/自訂正則） | +2 |
| 連結密度（≥3 個 URL） | +1 |
| 大量 @ 提及（≥2） | +1 |
| 弱帳號特徵（無 username/頭像/bio 至少 2 項） | +1 |
| 短時間消息爆發（≥5 條） | +1 |

**決策閾值**：
- **≥5 分** → `block`（私訊封鎖 / 群組踢除）
- **≥3 分** → `flag`（僅記錄標記，不採取動作——可在 `--report` 觀察）
- **<3 分** → `pass`（放行，避免誤封正常對話）

設計重點：單一弱信號（如一個 t.me 連結、單個「投資」詞）**不會**誤判；多個信號組合才升級。所有判定輸出顯示分數。

---

## 🔍 群組行為分析（v0.10.0）

`--listen` 模式下自動監控群組內可疑行為模式：

| 行為 | 閾值 | 動作 |
|------|------|------|
| 新成員進群後發連結 | 進群 5 分鐘內 | **自動踢除** |
| 刷屏廣告（含連結消息） | 120 秒內 ≥3 條 | **自動踢除** |
| 大量 @ 提及刷屏 | 120 秒內 ≥3 條 | **自動踢除** |

行為分析獨立於文字評分——即使連結文字不命中任何模式（短網址/圖片），行為特徵也會觸發。記錄標記為 `[behavior]` 原因。

---

## 📊 封鎖報告

```bash
# 每日報告
teleshield --report

# 每週報告（含每日趨勢）
teleshield --report week
```

報告內容：

```
📊 封鎖摘要 — 過去 24 小時
────────────────────────────
   總計封鎖: 12 人

   來源:
     • 私訊: 10 人
     • 群組: 2 人

   廣告類型 Top 5:
     • 投資理財: 5 次
     • 兼職詐騙: 3 次
     • 色情: 2 次
     • 賭博: 1 次
     • 英文 Spam: 1 次

   每日趨勢:
     2026-07-14: 12 人
```

---

## 🔍 廣告識別模式 / Spam Patterns

TeleShield 內建 **30+ 分級正則**（繁簡並收），加上學習模式可無限擴充：

| 級別 | 類別 | 範例 |
|------|------|------|
| 🔴 **高危** | 引流 | 加我微信、加V、V信、vx |
| 🔴 **高危** | 色情 | 裸聊、約炮、援交、成人 |
| 🔴 **高危** | 賭博 | 賭博、六合彩、下注、casino、betting |
| 🔴 **高危** | 兼職詐騙 | 兼職、刷單、日入、躺賺、在家工作 |
| 🟠 **中危** | 投資理財 | 投資、帶單、跟單、量化、穩賺、高回報 |
| 🟠 **中危** | 交易出售 | 出售、批發、代購、代發、清倉 |
| 🟠 **中危** | 假優惠 | 註冊送、免費領、紅包、優惠碼 |
| 🟠 **中危** | 刷量 | 點讚、刷粉、刷讚、漲粉 |
| 🟡 **低危** | 弱信號 | t.me 連結、@ 提及、tg 帳號、click here |

> **繁簡雙收**：每類模式同時覆蓋繁體與簡體（如 賭博/赌博、穩賺/稳赚），中港台廣告一網打盡。
> **誤封防護**：單字（出/博/彩/售）已移除，改語義簇組合；單一弱信號不判定。

---

## ⚙️ 安全性與權限

### 身分驗證

- 使用 **MTProto**（Telegram 官方協議）直接登入，非 Bot API
- Session 文件（`~/.teleshield/user.session`）使用 Telethon 內部加密儲存，且 **自動 chmod 600**（防止同機其他用戶讀取登入憑證）
- API 憑證僅儲存在本地 `~/.teleshield/config.json`（原子寫入 + chmod 600）或 `.env`
- 憑證**不接受命令行參數**（避免洩漏到 shell history），只走環境變數或交互輸入

### 權限需求

| 功能 | 所需權限 |
|------|---------|
| 私訊封鎖 | 無需額外權限（任何帳號皆可封鎖他人） |
| 群組踢除 | **群組管理員**（需 ban_users 權限） |
| 圖片 OCR | 本地 Tesseract，無需網路權限 |

### 風險說明

- Session 文件 = 你的 Telegram 身份，程式已自動設為 600 權限，請勿刪除或分享
- 群組踢除不可逆，使用 `--group-scan dry` 預覽再執行
- 所有敏感文件（config.json / block_log.json / learned_patterns.json / .env）均自動 chmod 600

---

## 🗂️ 專案結構 / Project Structure

```
TeleShield/
├── teleshield/            # Python 包
│   ├── __init__.py        # 版本定義（單一來源）
│   ├── __main__.py        # python -m teleshield 入口
│   ├── cli.py             # 命令解析與分派
│   ├── commands.py        # 核心動作（掃描/監聽/報告/名單）
│   ├── config.py          # 路徑/.env/存儲（原子寫 + chmod 600）
│   ├── patterns.py        # 分級廣告模式（severe/moderate/low，繁簡雙收）
│   ├── scoring.py         # 垃圾訊號評分引擎（v0.10.0）
│   ├── behavior.py        # 群組行為分析（v0.10.0）
│   ├── ocr.py             # 本地 Tesseract OCR（資料不外傳）
│   └── client.py          # Telethon 客戶端工廠
├── tests/                 # pytest 69 用例（誤封回歸/評分/行為/存儲）
├── .github/workflows/     # CI（ruff + pytest 3 版本 + build + 自動 Release）
├── pyproject.toml         # 打包配置（pip install teleshield）
├── install.sh             # 一鍵安裝腳本
├── .env.example           # 環境變數範例
├── README.md
└── LICENSE

~/.teleshield/             # 運行後自動生成（chmod 600）
├── user.session           # Telegram 登入 Session（加密 + 600）
├── config.json            # 設定 + 學習模式 + 名單
├── learned_patterns.json  # 學習模式獨立存儲
├── block_log.json         # 封鎖記錄（用於報告）
├── .env                   # 憑證（可選）
└── report_*.html          # HTML 報告（--report-html）
```

---

## 🧩 後續計劃 / Roadmap

**已完成（v0.10.0）**：
- [x] Phase 1 工程化：模組化重構、.env 配置、pytest 測試框架、CI/CD、pip 打包、install.sh
- [x] Phase 2 功能增強：多級規則引擎（分級正則）、垃圾訊號評分、群組行為分析、HTML 報告、community 名單 import/export
- [x] 安全審計修復：session/config 600 權限、憑證 env 化、繁簡雙收、誤封回歸測試

**待辦**：
- [ ] Phase 3：systemd 一鍵部署（常駐 + 日誌輪轉 + 自動重啟）
- [ ] 自動更新（檢查 GitHub Release + 校驗和）
- [ ] ML 分類器（本地樸素貝葉斯，用 block_log 訓練）
- [ ] Web Dashboard（查看封鎖統計 + 管理名單）
- [ ] 雲端名單同步（可選，黑白名單 → CF KV）

---

## 📄 License

[MIT](LICENSE) © 2026 WAHSUN

---

<div align="center">
  <sub>Made with ❤️ by WAHSUN · 讓 Telegram 清淨一點</sub>
</div>
