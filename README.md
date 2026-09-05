<div align="center">
  <h1>🛡️ TeleShield</h1>
  <p><strong>All-round Telegram spam firewall for your personal account</strong><br>
  <em>Private message blocking &amp; group moderation — all in one.</em></p>

  <p>
    <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <img src="https://img.shields.io/badge/telethon-1.44%2B-purple" alt="Telethon">
    <img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.github.com%2Frepos%2FC92D58%2FTeleShield%2Freleases%2Flatest&query=%24.tag_name&label=release&color=22C55E" alt="Release">
  </p>
  <p>
    <a href="https://teleshield.wahsun.org">🌐 Product Page</a>
    ·
    <a href="https://github.com/c92d58/TeleShield/releases/latest">📦 Download</a>
    ·
    <a href="https://github.com/c92d58/TeleShield#-quick-start">🚀 Quick Start</a>
  </p>
</div>

---

## 📋 Overview

**TeleShield** is a full-featured Telegram spam defense system covering **private DM blocking** and **group moderation**. Unlike Bot API bots, it logs in as you — handling the personal-account protection that bots simply cannot.

---

## ✨ Features

| Feature | Command | Description |
|---------|---------|-------------|
| **DM scan** | `--scan` | Scans recent non-contact conversations, matches spam patterns, blocks |
| **Group scan** | `--group-scan` | Scans recent group messages, kicks ad senders (admin required) |
| **Live listener** | `--listen` | Runs in the background, **watching DMs + groups** with second-level response |
| **Scoring engine** | built-in | Multi-signal spam scoring (tiered regex / link density / @ mentions / weak account traits / frequency) — automatically decides block / flag / pass |
| **Group behavior analysis** | built-in | New members posting links instantly, message-flood ads → auto kick |
| **Dry run** | `--dry-run` | Safe preview — shows results without blocking or kicking |
| **Image OCR** | built-in | Image-only ads → local Tesseract text recognition → pattern match; **data never leaves your machine** |
| **Learn mode** | `--learn <text>` | Manually flag spam; extracts keywords and generates regex patterns automatically |
| **Block reports** | `--report [day\|week]`, `--report-html` | Daily/weekly summaries + **HTML visual reports** (category stats / trends / details) |
| **Blacklist** | `--blacklist add\|remove\|list\|import\|export [id\|file]` | Auto block/kick on sight; **JSON exchange** for community lists |
| **Whitelist** | `--whitelist add\|remove\|list\|import\|export [id\|file]` | Whitelisted users are never scanned, blocked or kicked |
| **Status panel** | `--status` | Overview of blocks, kicks, lists and learn-mode state |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- Telegram API credentials from [my.telegram.org/apps](https://my.telegram.org/apps)
- (Optional) Tesseract OCR for image-ad detection

### Install

**Option 1: one-click install (recommended)**

```bash
git clone https://github.com/c92d58/TeleShield.git
cd TeleShield
bash install.sh          # checks dependencies + pip install + creates data directory
```

**Option 2: install via pip**

```bash
pip install "teleshield[ocr]"
# system dependency for OCR (optional but strongly recommended)
apt install tesseract-ocr tesseract-ocr-chi-sim
```

### First-time setup

```bash
teleshield --setup
```

Enter, in order:
1. `API ID` — from [my.telegram.org/apps](https://my.telegram.org/apps)
2. `API Hash` — same source
3. `Phone number` — with country code, e.g. `+852****5931`
4. `Verification code` — Telegram sends it to your phone

> **Security note**: credentials can also be written to `~/.teleshield/.env` (`TELESHIELD_API_ID` / `TELESHIELD_API_HASH` / `TELESHIELD_PHONE`), and `--setup` reads them automatically. **Never** pass credentials as command-line arguments — they would leak into your shell history.

After a successful login the session is saved under `~/.teleshield/`; you won't need to log in again.

### Basic usage

```bash
# ─── DM protection ───

# dry run first to preview the results
teleshield --dry-run

# actually scan recent DM spam
teleshield --scan

# start the live listener (background; full DM + group protection)
teleshield --listen

# ─── Group moderation ───

# scan all groups you moderate, kick ad senders
teleshield --group-scan

# ─── Learning & reports ───

# manually flag spam text so the program learns new patterns
teleshield --learn "加微信 abc123 投資穩賺日入過萬"

# view block summaries
teleshield --report         # last 24 hours
teleshield --report week    # last 7 days + trend
teleshield --report-html    # generates an HTML report (~/.teleshield/report_week.html)

# ─── List management ───

# whitelist (never blocked)
teleshield --whitelist add 12345678
teleshield --whitelist list

# blacklist (blocked on sight)
teleshield --blacklist add 87654321

# community list exchange (JSON)
teleshield --blacklist export my_list.json
teleshield --blacklist import community.json
teleshield --blacklist remove 87654321

# full status
teleshield --status
```

---

## 📖 Full Command Reference

| Command | Description |
|---------|-------------|
| `--setup` | First-time setup (credentials via **environment variables** `TELESHIELD_API_ID` / `TELESHIELD_API_HASH` / `TELESHIELD_PHONE` or interactive input — **not arguments**) |
| `--scan` | Scans non-contact DMs and blocks spam |
| `--dry-run` | Dry-run scan (does not actually block) |
| `--listen` | **Live listener** — DM blocking + group kicking + behavior analysis at once |
| `--group-scan` | Scans moderated groups and kicks ad senders |
| `--status` | Full status panel |
| `--report [day\|week]` | Block summary (default: day) |
| `--report-html [day\|week]` | Generates an HTML visual report (`~/.teleshield/report_<period>.html`) |
| `--learn <text>` | Manually flags spam text and learns new patterns |
| `--whitelist add\|remove\|list\|import\|export [user_id\|file]` | Whitelist management (import/export via JSON) |
| `--blacklist add\|remove\|list\|import\|export [user_id\|file]` | Blacklist management (import/export via JSON) |

---

## 👥 Group Moderation in Detail

TeleShield can automatically moderate any group where you have **admin rights**:

| Scenario | Behavior |
|----------|----------|
| `--listen` running | New group message → auto-detect → kick the ad sender |
| `--group-scan` | Scans the last 20 messages → batch kicks |
| Admins skipped | Group admins and the creator are never affected |
| Whitelist skipped | Whitelisted users are never kicked |
| 3-day window | Only messages from the last 3 days are examined |

Kicks use **ChatBannedRights(view_messages=True)** — Telegram's "block + remove", so the user cannot rejoin.

---

## 🧠 Learn Mode in Detail

When you meet a new spam pattern, use `--learn` to teach TeleShield:

```bash
# Example: flag an ad containing a URL
teleshield --learn "https://bit.ly/3XabcDe 免費領取 BTC"

# Example: flag a LINE/WeChat promotion
teleshield --learn "➕官方LINE：@free888 每日推薦飆股"
```

How learning works:

| Step | Description |
|------|-------------|
| 🔍 Extract keywords | filters stopwords, extracts high-value 2–6 character keywords |
| 🧩 Generate regex | builds reusable patterns from URLs, IDs and other structures |
| 💾 Persistent storage | saved in `config.json`, loaded on every start |
| 🔄 Immediate effect | `is_spam()` uses the new patterns right away |

View accumulated learning results via `--status`.

---

## 🧠 Spam Scoring Engine (v0.10.0)

Replaces single-layer regex decisions — multi-dimensional weighted scoring that automatically picks **block / flag / pass**:

| Signal | Points |
|--------|--------|
| Tiered regex hit (high risk / medium / low) | +3 / +2 / +1 (semantic clusters stack independently) |
| Learned patterns (keywords / custom regex) | +2 |
| Link density (≥3 URLs) | +1 |
| Heavy @ mentions (≥2) | +1 |
| Weak account traits (no username/avatar/bio — at least 2) | +1 |
| Message burst (≥5 in a short window) | +1 |

**Decision thresholds:**
- **≥5 points** → `block` (block in DMs / kick in groups)
- **≥3 points** → `flag` (record only, no action — watch it in `--report`)
- **<3 points** → `pass` (let through, avoiding false positives)

Design note: a single weak signal (one t.me link, one "投資/invest" word) will **not** cause a false positive; only combined signals escalate. Every verdict prints its score.

---

## 🔍 Group Behavior Analysis (v0.10.0)

In `--listen` mode, suspicious behavior patterns inside groups are monitored automatically:

| Behavior | Threshold | Action |
|----------|-----------|--------|
| New member posts a link after joining | within 5 minutes of joining | **auto kick** |
| Flooding ads (including links) | ≥3 messages in 120 s | **auto kick** |
| Mass @ mention flooding | ≥3 messages in 120 s | **auto kick** |

Behavior analysis is independent of text scoring — even when link text matches no pattern (short URLs / images), the behavior still triggers. Records are marked with the reason `[behavior]`.

---

## 📊 Block Reports

```bash
# daily report
teleshield --report

# weekly report (includes daily trend)
teleshield --report week
```

Sample report:

```
📊 Block summary — last 24 hours
────────────────────────────
   Total blocked: 12

   Source:
     • DMs: 10
     • Groups: 2

   Top 5 ad categories:
     • Investment scams: 5
     • Part-time job scams: 3
     • Adult content: 2
     • Gambling: 1
     • English spam: 1

   Daily trend:
     2026-07-14: 12
```

---

## 🔍 Spam Patterns

TeleShield ships with **30+ tiered regexes** (covering both Traditional and Simplified Chinese), infinitely extendable through learn mode:

| Level | Category | Examples |
|-------|----------|----------|
| 🔴 **High risk** | Traffic funneling | 加我微信、加V、V信、vx |
| 🔴 **High risk** | Adult content | 裸聊、約炮、援交、成人 |
| 🔴 **High risk** | Gambling | 賭博、六合彩、下注、casino、betting |
| 🔴 **High risk** | Job-scam | 兼職、刷單、日入、躺賺、在家工作 |
| 🟠 **Medium** | Investment | 投資、帶單、跟單、量化、穩賺、高回報 |
| 🟠 **Medium** | Selling | 出售、批發、代購、代發、清倉 |
| 🟠 **Medium** | Fake offers | 註冊送、免費領、紅包、優惠碼 |
| 🟠 **Medium** | Engagement farming | 點讚、刷粉、刷讚、漲粉 |
| 🟡 **Low** | Weak signals | t.me links、@ mentions、tg accounts、click here |

> **Both scripts covered**: every category matches Traditional and Simplified Chinese (e.g. 賭博/赌博, 穩賺/稳赚) — ads from HK, TW and CN are all caught.
> **False-positive protection**: single characters (出/博/彩/售) were removed in favor of semantic clusters; a lone weak signal never triggers a verdict.

---

## ⚙️ Security & Permissions

### Authentication

- Logs in via **MTProto** (Telegram's official protocol), not Bot API
- The session file (`~/.teleshield/user.session`) is stored with Telethon's internal encryption and **auto-chmod 600** (protects login credentials from other users on the same machine)
- API credentials are stored only in local `~/.teleshield/config.json` (atomic write + chmod 600) or `.env`
- Credentials are **never accepted as command-line arguments** (no shell-history leaks) — environment variables or interactive input only

### Required permissions

| Feature | Required permission |
|---------|---------------------|
| DM blocking | none (any account can block others) |
| Group kicking | **group admin** (needs ban_users) |
| Image OCR | local Tesseract, no network permission needed |

### Risk notes

- The session file *is* your Telegram identity — it is chmod 600 automatically; never delete or share it
- Group kicks are irreversible — preview with `--group-scan dry` first
- All sensitive files (config.json / block_log.json / learned_patterns.json / .env) are chmod 600 automatically

---

## 🗂️ Project Structure

```
TeleShield/
├── teleshield/            # Python package
│   ├── __init__.py        # version definition (single source)
│   ├── __main__.py        # python -m teleshield entry point
│   ├── cli.py             # command parsing & dispatch
│   ├── commands.py        # core actions (scan/listen/report/lists)
│   ├── config.py          # paths/.env/storage (atomic writes + chmod 600)
│   ├── patterns.py        # tiered spam patterns (severe/moderate/low, both scripts)
│   ├── scoring.py         # spam scoring engine (v0.10.0)
│   ├── behavior.py        # group behavior analysis (v0.10.0)
│   ├── ocr.py             # local Tesseract OCR (data stays local)
│   └── client.py          # Telethon client factory
├── tests/                 # 69 pytest cases (false-positive regression/scoring/behavior/storage)
├── .github/workflows/     # CI (ruff + pytest on 3 versions + build + auto Release)
├── pyproject.toml         # packaging (pip install teleshield)
├── install.sh             # one-click install script
├── .env.example           # environment variable example
├── README.md
└── LICENSE

~/.teleshield/             # auto-generated at runtime (chmod 600)
├── user.session           # Telegram login session (encrypted + 600)
├── config.json            # settings + learned patterns + lists
├── learned_patterns.json  # learn-mode patterns, separate storage
├── block_log.json         # block records (used for reports)
├── .env                   # credentials (optional)
└── report_*.html          # HTML reports (--report-html)
```

---

## 🧩 Roadmap

**Done (v0.10.0):**
- [x] Phase 1 engineering: modular refactor, .env configuration, pytest framework, CI/CD, pip packaging, install.sh
- [x] Phase 2 features: tiered rule engine, spam scoring, group behavior analysis, HTML reports, community list import/export
- [x] Security-audit fixes: session/config 600 permissions, env-based credentials, both-script coverage, false-positive regression tests

**Planned:**
- [ ] Phase 3: systemd one-click deployment (daemon + log rotation + auto-restart)
- [ ] Auto-update (checks GitHub Release + checksum)
- [ ] ML classifier (local Naive Bayes trained on block_log)
- [ ] Web dashboard (view block stats + manage lists)
- [ ] Cloud list sync (optional, black/white lists → CF KV)

---

## 📄 License

[MIT](LICENSE) © 2026 WAHSUN

---

<div align="center">
  <sub>Made with ❤️ by WAHSUN · Keep Telegram clean</sub>
</div>
