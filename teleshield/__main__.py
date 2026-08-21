"""python -m teleshield 入口。"""

import asyncio

from .cli import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
