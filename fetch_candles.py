"""
Pull Deriv candles for the hourly Claude analysis bot.

Runs inside GitHub Actions and writes JSON files into data/ that the Claude
scheduled task reads. Tries Deriv's new public gateway first, then the legacy
endpoints, with retries.

Env vars (all optional):
  DERIV_SYMBOL   Deriv symbol code. Default R_100 (Volatility 100 Index, 2s ticks)
  DERIV_APP_ID   App id for the legacy endpoints. Default 1089 (public test id)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import websockets

SYMBOL = os.getenv("DERIV_SYMBOL", "R_100")
APP_ID = os.getenv("DERIV_APP_ID", "1089")

ENDPOINTS = [
    "wss://api.derivws.com/trading/v1/options/ws/public",          # new public gateway, no auth
    f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",          # legacy
    f"wss://ws.binaryws.com/websockets/v3?app_id={APP_ID}",         # legacy alias
    f"wss://green.derivws.com/websockets/v3?app_id={APP_ID}",       # legacy alias
]

# granularity (seconds) -> (file tag, how many candles)
FRAMES = {
    3600: ("1h", 500),
    14400: ("4h", 300),
    900: ("15m", 300),
}

OUT = Path(__file__).parent / "data"
UA = "Mozilla/5.0 (X11; Linux x86_64) vol100-bot/1.1"


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
        if "error" in msg:
            raise RuntimeError(f"Deriv error: {msg['error']}")
        if msg.get("msg_type") == "candles":
            return msg.get("candles") or []


async def pull_all(url):
    results = {}
    async with websockets.connect(
        url,
        ping_interval=20,
        open_timeout=20,
        origin="https://app.deriv.com",
        user_agent_header=UA,
    ) as ws:
        for gran, (tag, count) in FRAMES.items():
            candles = await fetch(ws, gran, count)
            rows = []
            for c in candles:
                try:
                    rows.append({
                        "t": int(c["epoch"]),
                        "o": float(c["open"]),
                        "h": float(c["high"]),
                        "l": float(c["low"]),
                        "c": float(c["close"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue  # skip incomplete candles
            if not rows:
                raise RuntimeError(f"no candles returned for {tag}")
            results[tag] = (gran, rows)
    return results


async def main():
    OUT.mkdir(exist_ok=True)
    errors = []
    for attempt in range(3):
        for url in ENDPOINTS:
            try:
                results = await pull_all(url)
            except Exception as e:  # noqa: BLE001
                errors.append(f"attempt {attempt + 1} {url.split('?')[0]}: {type(e).__name__}: {e}")
                print(errors[-1], flush=True)
                continue
            for tag, (gran, rows) in results.items():
                payload = {
                    "symbol": SYMBOL,
                    "granularity": gran,
                    "fetched_at": int(time.time()),
                    "source": url.split("?")[0],
                    "candles": rows,
                }
                path = OUT / f"{SYMBOL}_{tag}.json"
                path.write_text(json.dumps(payload))
                print(f"OK via {url.split('?')[0]} -> {path.name}: "
                      f"{len(rows)} candles, last close {rows[-1]['c']}")
            return
        await asyncio.sleep(10 * (attempt + 1))
    print("All endpoints failed:\n" + "\n".join(errors), file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
