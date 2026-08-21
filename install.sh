#!/usr/bin/env bash
# TeleShield 一鍵安裝腳本（pip 安裝 + 依賴檢查）
set -euo pipefail

c_bright="\033[1m"; c_green="\033[32m"; c_dim="\033[2m"; c_red="\033[31m"; c_none="\033[0m"
ok()  { echo -e " ${c_green}✓${c_none} $*"; }
warn(){ echo -e " ${c_red}⚠${c_none} $*"; }
dim() { echo -e " ${c_dim}$*${c_none}"; }

echo -e "${c_bright}TeleShield 安裝${c_none}\n"

# 1. 檢查 python3
if ! command -v python3 >/dev/null 2>&1; then
    warn "未找到 python3，請先安裝：apt install -y python3 python3-pip"
    exit 1
fi
ok "Python: $(python3 --version 2>&1)"

# 2. 可選：Tesseract OCR（圖片廣告辨識）
if command -v tesseract >/dev/null 2>&1; then
    langs=$(tesseract --list-langs 2>/dev/null | tr -d '\n')
    if [[ "$langs" != *chi_sim* ]]; then
        warn "Tesseract 缺少中文語言包（chi_sim），圖片中文廣告可能無法辨識"
        dim "  安裝：apt install -y tesseract-ocr-chi-sim"
    else
        ok "Tesseract OCR（含中文）"
    fi
else
    warn "未找到 tesseract（可選，用於圖片廣告 OCR）"
    dim "  安裝：apt install -y tesseract-ocr tesseract-ocr-chi-sim"
fi

# 3. 安裝套件（含 OCR 依賴）
echo
dim "安裝 teleshield（含 OCR 依賴）..."
pip3 install --user "teleshield[ocr]" || pip3 install --user .
ok "安裝完成"

# 4. 建立數據目錄與 .env 範例
TELESHIELD_HOME="${TELESHIELD_HOME:-$HOME/.teleshield}"
mkdir -p "$TELESHIELD_HOME"
chmod 700 "$TELESHIELD_HOME"
if [[ ! -f "$TELESHIELD_HOME/.env" ]]; then
    cp .env.example "$TELESHIELD_HOME/.env" 2>/dev/null || true
    chmod 600 "$TELESHIELD_HOME/.env" 2>/dev/null || true
    dim "已建立 $TELESHIELD_HOME/.env（請填入 API 憑證）"
fi

echo
ok "安裝完成！"
echo -e "  1. 編輯 $TELESHIELD_HOME/.env 填入 API 憑證"
echo -e "  2. 執行：${c_bright}teleshield --setup${c_none}"
echo -e "  3. 執行：${c_bright}teleshield --listen${c_none} 開始監聽"
