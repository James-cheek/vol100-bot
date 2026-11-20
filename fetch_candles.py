"""
Pull Deriv candles for the hourly Claude analysis bot.

Runs inside GitHub Actions (which can reach Deriv's API) and writes JSON files
into data/ that the Claude scheduled task reads.

Env vars (all optional):
  DERIV_SYMBOL   Deriv symbol code. Default R_100 (Volatility 100 Index, 2s ticks)
  DERIV_APP_ID   Your Deriv app id. Default 1089 (Deriv's public test id)
"""
import asyncio
import json
import os
import time
from pathlib import Path

import websockets

SYMBOL = os.getenv("DERIV_SYMBOL", "R_100")
APP_ID = os.getenv("DERIV_APP_ID", "1089")
URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# granularity (seconds) -> (file tag, how many candles)
FRAMES = {
    3600: ("1h", 500),
    14400: ("4h", 300),
    900: ("15m", 300),
}

OUT = Path(__file__).parent / "data"


async def fetch(ws, granularity, count):
    req = {
        "ticks_history": SYMBOL,
        "style": "candles",
        "granularity": granularity,
        "count": count,
        "end": "latest",
        "adjust_start_time": 1,
    }
    await ws.send(json.dumps(req))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if msg.get("msg_type") == "candles":
            return msg["candles"]
        if "error" in msg:
            raise RuntimeError(f"Deriv error: {msg['error']}")


async def main():
    OUT.mkdir(exist_ok=True)
    async with websockets.connect(URL, ping_interval=20) as ws:
        for gran, (tag, count) in FRAMES.items():
            candles = await fetch(ws, gran, count)
            rows = [
                {
                    "t": int(c["epoch"]),
                    "o": float(c["open"]),
                    "h": float(c["high"]),
                    "l": float(c["low"]),
                    "c": float(c["close"]),
                }
                for c in candles
            ]
            payload = {
                "symbol": SYMBOL,
                "granularity": gran,
                "fetched_at": int(time.time()),
                "candles": rows,
            }
            path = OUT / f"{SYMBOL}_{tag}.json"
            path.write_text(json.dumps(payload))
            print(f"{path.name}: {len(rows)} candles, last close {rows[-1]['c']}")


if __name__ == "__main__":
    asyncio.run(main())
