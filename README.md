# vol100-bot: hourly 1H Claude analysis for Deriv Volatility 100 Index

How it works

1. GitHub Action (`.github/workflows/fetch-candles.yml`) runs every hour at :07 UTC, pulls R_100 candles (1H, 4H, 15m) from Deriv's API with `fetch_candles.py`, and commits them to `data/`.
2. A Claude scheduled task runs at 20 minutes past every hour, clones this repo, runs `analyze.py`, looks at the chart, and sends you the summary, levels, watch-outs and trades, plus the marked-up chart.

## Files

| File | What it does |
|---|---|
| `fetch_candles.py` | Pulls candles from Deriv WebSocket API into `data/` |
| `.github/workflows/fetch-candles.yml` | Runs the fetcher hourly and commits the data |
| `analyze.py` | Trend, structure/BOS, S/R zones, PDH/PDL, equal highs/lows, RSI divergence, volatility regime, candle patterns, trade candidates with SL/TP/RR, chart.png |
| `TASK_PROMPT.md` | The instructions the Claude scheduled task runs each hour |

## Setup (about 5 minutes)

1. github.com > `+` (top right) > **New repository** > name `vol100-bot` > **Public** > **Create repository**.
   Public is simplest: Claude reads it without extra access, and it only holds public market candles.
2. Push the files (Terminal on your Mac, inside the unzipped folder):
   ```
   cd ~/Downloads/vol100-bot
   git init -b main
   git add .
   git commit -m "vol100 bot"
   git remote add origin https://github.com/YOUR_USERNAME/vol100-bot.git
   git push -u origin main
   ```
3. Repo > **Settings** > **Actions** > **General** > scroll to **Workflow permissions** > select **Read and write permissions** > **Save**.
4. Repo > **Actions** tab > **Fetch Deriv candles** (left) > **Run workflow** > **Run workflow**. After about 30 seconds a `data/` folder with `R_100_1h.json`, `R_100_4h.json`, `R_100_15m.json` appears.
5. Send Claude your GitHub username. Claude creates the hourly scheduled task.

## Optional

- Own Deriv app id (instead of the public 1089): register an app on api.deriv.com, then repo > **Settings** > **Secrets and variables** > **Actions** > **Variables** tab > **New repository variable** > name `DERIV_APP_ID`.
- Different index: add variable `DERIV_SYMBOL` (`R_75`, `1HZ100V`, etc.) and tell Claude to change the task's `--symbol`.
- Run locally: `pip install -r requirements.txt && python fetch_candles.py && python analyze.py`.

Note: GitHub can delay scheduled Actions by a few minutes at busy times. The Claude report flags DATA STALE if the newest closed candle is missing.
