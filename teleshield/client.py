"""Telethon 客戶端工廠：統一 session/憑證來源。"""

from __future__ import annotations

from telethon import TelegramClient

from .config import SESSION_FILE

__all__ = ["get_client"]


def get_client(cfg: dict) -> TelegramClient:
    """從 config 創建 Telethon 客戶端（session 存於 TELESHIELD_HOME）。"""
    return TelegramClient(
        str(SESSION_FILE),
        int(cfg["api_id"]),
        cfg["api_hash"],
    )
