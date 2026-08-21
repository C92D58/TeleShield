"""圖片 OCR：本地 Tesseract 辨識（資料不外傳）。"""

from __future__ import annotations

import os
import tempfile

__all__ = ["ocr_image", "check_photo"]


def ocr_image(image_path: str) -> str:
    """對本地圖片執行 OCR，返回文字。失敗返回空字串。"""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception:
        return ""


async def check_photo(client, msg) -> str:
    """下載消息圖片 → OCR → 清理臨時檔。返回辨識文字（無圖片/失敗為空）。"""
    if not msg or not msg.photo:
        return ""
    tmp = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp = f.name
        await client.download_media(msg, file=tmp)
        text = ocr_image(tmp)
        return text
    except Exception:
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
