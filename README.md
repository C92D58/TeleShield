<div align="center">
  <h1>🛡️ TeleShield</h1>
  <p><strong>Telegram 全能廣告封鎖守衛</strong><br>
  <em>Your personal Telegram spam firewall — private messages & group management, all in one.</em></p>

  <p>
    <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <img src="https://img.shields.io/badge/telethon-1.44%2B-purple" alt="Telethon">
    <img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.github.com%2Frepos%2Fc92d58%2FTeleShield%2Freleases%2Flatest&query=%24.tag_name&label=release&color=22C55E" alt="Release">
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

## 🖥️ Desktop App（測試版）

本 fork 正在把 TeleShield 包裝成一般使用者可以直接安裝的桌面應用程式：

- PySide6 GUI，不需要使用者執行 CLI
- Telegram 個人帳號登入流程
- 關閉主視窗只會隱藏到系統匣，背景防護仍然執行
- 系統匣選單可重新開啟、開始／停止防護或真正結束程式
- 可選擇登入作業系統時自動啟動，並自動開始防護
- 可從 GUI 掃描近期既有私訊或管理員群組訊息
- 歷史掃描預設為 dry-run 預覽；實際封鎖／踢除前會要求明確確認
- 歷史掃描在背景工作執行，可查看進度並停止掃描
- Session、設定與封鎖紀錄放在使用者資料目錄，不寫入 App bundle

> 歷史掃描需要先停止即時防護，避免同一個 Telegram Session 被兩個工作同時使用。
> 私訊掃描目前檢查最近 30 個對話、每個最近 5 則、14 天內訊息；群組掃描檢查可管理群組最近 20 則、3 天內訊息。

### GitHub Actions 測試版

本機不需要打包。到 GitHub Actions 手動執行 **Desktop test builds** workflow，或推送到 `main`／`feat/*` 分支後等待建置完成，即可下載：

```text
macOS-intel.dmg
macOS-arm.dmg
```

目前是未簽署測試版，macOS 可能顯示「無法驗證開發者」。此階段先用右鍵「打開」允許測試；正式發布再加入 Apple Developer signing 與 notarization。

> 測試版 DMG 目前不內含 Tesseract 執行檔與中文語言資料；文字廣告防護可直接使用，圖片 OCR 需另做 bundled OCR 版本。  
> DMG 內的 App 仍然是以使用者個人 Telegram 身份執行。請只在自己的電腦使用，並妥善保護 Session。

---

## ✨ 功能 / Features

| 功能 | 命令 | 說明 |
|------|------|------|
| 🔍 **私訊掃描** | `--scan` | 掃描近期非聯絡人對話，比對廣告模式並封鎖 |
| 👥 **群組掃描** | `--group-scan` | 掃描群組近期訊息，踢除發廣告的成員（需管理員權限） |
| 🛡️ **即時監聽** | `--listen` | 後台常駐，**同時監控私訊+群組**，秒級響應 |
| 🧪 **試運行** | `--dry-run` | 安全預覽，只顯示結果不實際封鎖/踢除 |
| 📸 **圖片 OCR** | 內建 | 純圖片廣告 → Tesseract 本地辨識文字 → 模式比對，**資料不外傳** |
| 🧠 **學習模式** | `--learn <文字>` | 手動標記廣告，自動提取關鍵詞+生成正則模式 |
| 📊 **封鎖報告** | `--report [day\|week]` | 生成每日/每週封鎖摘要，含類型統計+趨勢 |
| ⚫ **黑名單** | `--blacklist add\|remove\|list [id]` | 加入黑名單後，在私訊/群組中**自動封鎖或踢除** |
| ⚪ **白名單** | `--whitelist add\|remove\|list [id]` | 白名單用戶永不被掃描、封鎖或踢除 |
| 📊 **狀態面板** | `--status` | 一覽封鎖數、踢除數、名單和學習模式狀態 |

---

## 🚀 快速開始 / Quick Start

### 前置需求 / Prerequisites

- Python 3.9+
- Telegram API 憑證（[my.telegram.org/apps](https://my.telegram.org/apps)）
- （選用）Tesseract OCR 用於圖片廣告辨識

### 安裝 / Install

```bash
# 克隆倉庫
git clone https://github.com/c92d58/TeleShield.git
cd TeleShield

# 安裝核心依賴
pip install telethon

# 圖片 OCR 支援（選用，強烈建議安裝）
apt install tesseract-ocr tesseract-ocr-chi-sim
pip install pytesseract Pillow
```

### 首次設定 / First-time Setup

```bash
python teleshield.py --setup
```

依序輸入：
1. `API ID` — 從 [my.telegram.org/apps](https://my.telegram.org/apps) 取得
2. `API Hash` — 同上
3. `手機號碼` — 含國碼，如 `+852****5931`
4. `驗證碼` — Telegram 會發送驗證碼到你手機

登入成功後自動儲存 Session，下次不需重複登入。

### 基本用法 / Usage

```bash
# ─── 私訊防護 ───

# 先試運行看看結果
python teleshield.py --dry-run

# 實際掃描近期待處理的廣告
python teleshield.py --scan

# 啟動即時監聽（後台常駐，私訊+群組全保護）
python teleshield.py --listen

# ─── 群組管理 ───

# 掃描所有管理中的群組，踢除廣告發送者
python teleshield.py --group-scan

# ─── 學習與報告 ───

# 手動標記廣告文字，讓程式學習新模式
python teleshield.py --learn "加微信 abc123 投資穩賺日入過萬"

# 查看封鎖摘要
python teleshield.py --report         # 過去 24 小時
python teleshield.py --report week    # 過去 7 天 + 趨勢

# ─── 名單管理 ───

# 白名單（永不封鎖）
python teleshield.py --whitelist add 12345678
python teleshield.py --whitelist list

# 黑名單（見一個封一個）
python teleshield.py --blacklist add 87654321
python teleshield.py --blacklist remove 87654321

# 查看完整狀態
python teleshield.py --status
```

---

## 📖 完整命令參考 / Full Command Reference

| 命令 | 說明 |
|------|------|
| `--setup [api_id] [api_hash] [phone] [code]` | 首次設定或重新登入 |
| `--scan` | 掃描非聯絡人私訊，封鎖廣告 |
| `--dry-run` | 試運行掃描（不實際封鎖） |
| `--listen` | **即時監聽模式** — 私訊封鎖 + 群組踢除同時運作 |
| `--group-scan` | 掃描管理中的群組，踢除廣告發送者 |
| `--status` | 查看完整狀態面板 |
| `--report [day\|week]` | 封鎖摘要報告（預設 day） |
| `--learn <文字>` | 手動標記廣告文字，自動學習新模式 |
| `--whitelist add\|remove\|list [user_id]` | 白名單管理 |
| `--blacklist add\|remove\|list [user_id]` | 黑名單管理 |

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
python teleshield.py --learn "https://bit.ly/3XabcDe 免費領取 BTC"

# 範例：標記一個 LINE/微信推廣
python teleshield.py --learn "➕官方LINE：@free888 每日推薦飆股"
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

## 📊 封鎖報告

```bash
# 每日報告
python teleshield.py --report

# 每週報告（含每日趨勢）
python teleshield.py --report week
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

TeleShield 內建 20+ 正則表達式，加上學習模式可無限擴充：

| 類別 | 範例關鍵字 |
|------|-----------|
| 💰 投資理財 | 投資、帶單、跟單、量化、穩賺 |
| 💼 兼職詐騙 | 兼職、刷單、日入、躺賺、被動收入 |
| 🔞 色情 | 裸聊、約炮、援交、成人 |
| 🎰 賭博 | 賭、博彩、casino、betting |
| 📣 群組推廣 | @xxx、t.me/xxx、加微信 |
| 🎁 假優惠 | 免費領、紅包、優惠碼、推廣碼 |
| 📢 英文 Spam | promotion, giveaway, earn money, free crypto |
| 📸 **圖片 OCR** | 純圖片廣告 → Tesseract 本地辨識 → 模式比對 |
| 🧠 **學習模式** | 你標記什麼，它就學會什麼 |

---

## ⚙️ 安全性與權限

### 身分驗證

- 使用 **MTProto**（Telegram 官方協議）直接登入，非 Bot API
- Session 文件（`user.session`）使用 Telethon 內部加密儲存
- API 憑證僅儲存在本地 `config.json`

### 權限需求

| 功能 | 所需權限 |
|------|---------|
| 私訊封鎖 | 無需額外權限（任何帳號皆可封鎖他人） |
| 群組踢除 | **群組管理員**（需 ban_users 權限） |
| 圖片 OCR | 本地 Tesseract，無需網路權限 |

### 風險說明

- Session 文件 = 你的 Telegram 身份，務必保護好
- `chmod 600 ~/.tg-sessions/*` 限制檔案權限
- 群組踢除不可逆，使用 `--group-scan dry` 預覽再執行

---

## 🗂️ 專案結構 / Project Structure

```
TeleShield/
├── teleshield.py       # 主程式（完整功能）
├── README.md           # 本文件
├── LICENSE             # MIT 授權
└── .gitignore

.tg-sessions/           # 運行後自動生成
├── user.session        # Telegram 登入 Session（已加密）
├── config.json         # 設定 + 學習模式 + 名單
└── block_log.json      # 封鎖記錄（用於報告）
```

---

## 🧩 後續計劃 / Roadmap

- [x] 圖片廣告辨識：Tesseract 本地 OCR
- [x] 群組管理：自動踢除發廣告的群組成員
- [x] 學習模式：手動標記後自動歸納特徵
- [x] 封鎖報告：每日/每週封鎖摘要報告
- [x] 白名單 / 黑名單管理指令
- [ ] Docker 一鍵部署
- [ ] Web Dashboard（查看封鎖統計 + 管理名單）
- [ ] Telegram Bot 指令管理（/block, /whitelist 等）

---

## 📄 License

[MIT](LICENSE) © 2026 WAHSUN

---

<div align="center">
  <sub>Made with ❤️ by WAHSUN · 讓 Telegram 清淨一點</sub>
</div>
