"""
1H analysis engine for the Claude trading bot.

Reads data/<SYMBOL>_{1h,4h,15m}.json (written by fetch_candles.py), works out
trend, volatility regime, support/resistance, liquidity, momentum and candidate
trades, then writes:
  out/analysis.json   structured facts for Claude to read
  out/chart.png       1H chart with levels and the top trade idea drawn on it

Usage: python analyze.py [--symbol R_100] [--data data] [--out out]
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PIVOT_K = 3          # bars each side for a swing high/low
CHART_BARS = 120     # 1H bars shown on the chart
NAMES = {
    "R_10": "Volatility 10 Index", "R_25": "Volatility 25 Index",
    "R_50": "Volatility 50 Index", "R_75": "Volatility 75 Index",
    "R_100": "Volatility 100 Index",
    "1HZ10V": "Volatility 10 (1s) Index", "1HZ25V": "Volatility 25 (1s) Index",
    "1HZ50V": "Volatility 50 (1s) Index", "1HZ75V": "Volatility 75 (1s) Index",
    "1HZ100V": "Volatility 100 (1s) Index",
}


# ---------------------------------------------------------------- data
def load(data_dir, symbol, tag):
    p = Path(data_dir) / f"{symbol}_{tag}.json"
    raw = json.loads(p.read_text())
    df = pd.DataFrame(raw["candles"]).rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close"})
    df["time"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    gran, fetched = raw["granularity"], raw["fetched_at"]
    df["closed"] = (df["t"] + gran) <= fetched
    return df, gran, fetched


# ---------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def add_ind(df):
    df = df.copy()
    for n in (20, 50, 200):
        df[f"ema{n}"] = ema(df["close"], n)
    df["atr"] = atr(df)
    df["rsi"] = rsi(df["close"])
    mid = df["close"].rolling(20).mean()
    sd = df["close"].rolling(20).std()
    df["bbw"] = (4 * sd) / mid
    return df


# ---------------------------------------------------------------- structure
def swings(df, k=PIVOT_K):
    """Confirmed swing highs/lows as lists of (index_pos, price, time)."""
    h, l = df["high"].values, df["low"].values
    sh, sl = [], []
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            sh.append((i, h[i], df.index[i]))
        if l[i] == l[i - k:i + k + 1].min():
            sl.append((i, l[i], df.index[i]))
    return sh, sl


def structure(df, sh, sl):
    out = {"trend": "range/mixed", "last_bos": None}
    if len(sh) >= 2 and len(sl) >= 2:
        hh, hl = sh[-1][1] > sh[-2][1], sl[-1][1] > sl[-2][1]
        lh, ll = sh[-1][1] < sh[-2][1], sl[-1][1] < sl[-2][1]
        if hh and hl:
            out["trend"] = "uptrend (HH + HL)"
        elif lh and ll:
            out["trend"] = "downtrend (LH + LL)"
        elif hh and ll:
            out["trend"] = "expanding range"
        elif lh and hl:
            out["trend"] = "contracting range (coil)"
        out["swing_highs"] = [round(x[1], 2) for x in sh[-3:]]
        out["swing_lows"] = [round(x[1], 2) for x in sl[-3:]]

    # most recent break of structure on a candle close
    closes = df["close"].values
    events = []
    for i, p, t in sh[-8:]:
        for j in range(i + PIVOT_K, len(df)):
            if closes[j] > p:
                events.append((j, "bullish BOS", p, df.index[j]))
                break
    for i, p, t in sl[-8:]:
        for j in range(i + PIVOT_K, len(df)):
            if closes[j] < p:
                events.append((j, "bearish BOS", p, df.index[j]))
                break
    if events:
        j, kind, p, t = max(events, key=lambda e: e[0])
        out["last_bos"] = {"type": kind, "level": round(p, 2),
                           "bars_ago": len(df) - 1 - j, "time": t.isoformat()}
    return out


def cluster_levels(sh, sl, tol, n_bars):
    pts = [(p, i, "H") for i, p, _ in sh] + [(p, i, "L") for i, p, _ in sl]
    pts.sort()
    clusters, cur = [], []
    for p in pts:
        # new zone when the gap is big OR the zone would get wider than 2x tol
        if cur and (p[0] - cur[-1][0] > tol or p[0] - cur[0][0] > 2 * tol):
            clusters.append(cur)
            cur = []
        cur.append(p)
    if cur:
        clusters.append(cur)
    levels = []
    for c in clusters:
        prices = np.array([x[0] for x in c])
        last_i = max(x[1] for x in c)
        recency = 1 - (n_bars - 1 - last_i) / n_bars
        levels.append({
            "price": round(float(prices.mean()), 2),
            "touches": len(c),
            "bars_since_touch": int(n_bars - 1 - last_i),
            "score": round(len(c) + 2 * recency, 2),
        })
    return levels


def equal_hl(sh, sl, tol, df):
    """Untaken equal highs / lows (liquidity pools)."""
    res = []
    hi_after = lambda i: df["high"].values[i + 1:].max() if i + 1 < len(df) else -np.inf
    lo_after = lambda i: df["low"].values[i + 1:].min() if i + 1 < len(df) else np.inf
    recent_h, recent_l = sh[-8:], sl[-8:]
    for a in range(len(recent_h)):
        for b in range(a + 1, len(recent_h)):
            pa, pb = recent_h[a][1], recent_h[b][1]
            if abs(pa - pb) <= tol:
                top = max(pa, pb)
                if hi_after(recent_h[b][0]) <= top:
                    res.append({"type": "equal highs", "price": round(top, 2)})
    for a in range(len(recent_l)):
        for b in range(a + 1, len(recent_l)):
            pa, pb = recent_l[a][1], recent_l[b][1]
            if abs(pa - pb) <= tol:
                bot = min(pa, pb)
                if lo_after(recent_l[b][0]) >= bot:
                    res.append({"type": "equal lows", "price": round(bot, 2)})
    seen, uniq = set(), []
    for r in res:
        key = (r["type"], round(r["price"] / max(tol, 1e-9)))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def divergence(df, sh, sl):
    r = df["rsi"].values
    out = None
    if len(sh) >= 2:
        (i1, p1, _), (i2, p2, _) = sh[-2], sh[-1]
        if p2 > p1 and r[i2] < r[i1] - 2:
            out = f"bearish RSI divergence (HH {p2:.2f} vs lower RSI)"
    if len(sl) >= 2:
        (i1, p1, _), (i2, p2, _) = sl[-2], sl[-1]
        if p2 < p1 and r[i2] > r[i1] + 2:
            if out is None or sl[-1][0] > sh[-1][0]:
                out = f"bullish RSI divergence (LL {p2:.2f} vs higher RSI)"
    return out


def candle_pattern(df):
    c, p = df.iloc[-1], df.iloc[-2]
    body = abs(c.close - c.open)
    rng = c.high - c.low or 1e-9
    up_w = c.high - max(c.close, c.open)
    dn_w = min(c.close, c.open) - c.low
    pats = []
    if c.close > c.open and p.close < p.open and c.close >= p.open and c.open <= p.close:
        pats.append("bullish engulfing")
    if c.close < c.open and p.close > p.open and c.close <= p.open and c.open >= p.close:
        pats.append("bearish engulfing")
    if dn_w > 2 * body and dn_w > 0.55 * rng:
        pats.append("hammer / bullish pin (long lower wick)")
    if up_w > 2 * body and up_w > 0.55 * rng:
        pats.append("shooting star / bearish pin (long upper wick)")
    if c.high <= p.high and c.low >= p.low:
        pats.append("inside bar (compression)")
    if body < 0.15 * rng:
        pats.append("doji (indecision)")
    return pats


# ---------------------------------------------------------------- trades
def rr(entry, sl, tp):
    risk = abs(entry - sl)
    return round(abs(tp - entry) / risk, 2) if risk else None


def build_trades(price, a, bias, sup, res):
    trades = []
    r1 = res[0]["price"] if res else None
    r2 = res[1]["price"] if len(res) > 1 else None
    s1 = sup[0]["price"] if sup else None
    s2 = sup[1]["price"] if len(sup) > 1 else None

    def add(name, side, typ, entry, sl, tp1, tp2, why, invalid):
        if None in (entry, sl, tp1):
            return
        if side == "BUY" and not (sl < entry < tp1):
            return
        if side == "SELL" and not (tp1 < entry < sl):
            return
        trades.append({
            "name": name, "side": side, "order": typ,
            "entry": round(entry, 2), "sl": round(sl, 2),
            "tp1": round(tp1, 2), "tp2": round(tp2, 2) if tp2 else None,
            "risk_points": round(abs(entry - sl), 2),
            "rr_tp1": rr(entry, sl, tp1), "rr_tp2": rr(entry, sl, tp2) if tp2 else None,
            "distance_from_price_atr": round(abs(entry - price) / a, 2),
            "aligned_with_bias": (bias == "bullish" and side == "BUY") or
                                 (bias == "bearish" and side == "SELL"),
            "why": why, "invalidation": invalid,
        })

    if s1 and bias in ("bullish", "neutral"):
        e = s1 + 0.1 * a
        sl = s1 - 0.6 * a
        add("Pullback buy at support", "BUY", "buy limit", e, sl,
            r1 if r1 else e + 2 * (e - sl), r2 if r2 else e + 3 * (e - sl),
            f"buy the retest of support {s1:.2f}",
            f"1H close below {sl:.2f}")
    if r1 and bias in ("bearish", "neutral"):
        e = r1 - 0.1 * a
        sl = r1 + 0.6 * a
        add("Pullback sell at resistance", "SELL", "sell limit", e, sl,
            s1 if s1 else e - 2 * (sl - e), s2 if s2 else e - 3 * (sl - e),
            f"sell the retest of resistance {r1:.2f}",
            f"1H close above {sl:.2f}")
    if r1 and bias in ("bullish", "neutral"):
        e = r1 + 0.15 * a
        sl = e - 1.2 * a
        add("Breakout buy", "BUY", "buy stop", e, sl,
            r2 if r2 and r2 - e > 1.2 * (e - sl) else e + 2 * (e - sl),
            e + 3 * (e - sl),
            f"momentum through resistance {r1:.2f}",
            f"1H close back below {r1:.2f} (failed breakout)")
    if s1 and bias in ("bearish", "neutral"):
        e = s1 - 0.15 * a
        sl = e + 1.2 * a
        add("Breakdown sell", "SELL", "sell stop", e, sl,
            s2 if s2 and e - s2 > 1.2 * (sl - e) else e - 2 * (sl - e),
            e - 3 * (sl - e),
            f"momentum through support {s1:.2f}",
            f"1H close back above {s1:.2f} (failed breakdown)")

    trades.sort(key=lambda t: (not t["aligned_with_bias"], -(t["rr_tp1"] or 0)))
    return trades


# ---------------------------------------------------------------- chart
def draw_chart(df, levels_sup, levels_res, keys, trade, title, path):
    d = df.tail(CHART_BARS)
    x = np.arange(len(d))
    bg, grid, up_c, dn_c = "#131722", "#2a2e39", "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    w = 0.6
    for i, (_, r) in enumerate(d.iterrows()):
        col = up_c if r.close >= r.open else dn_c
        ax.vlines(i, r.low, r.high, color=col, lw=1)
        ax.add_patch(plt.Rectangle((i - w / 2, min(r.open, r.close)), w,
                                   max(abs(r.close - r.open), 1e-9), color=col))
    for n, c in ((20, "#f7c948"), (50, "#2962ff"), (200, "#e040fb")):
        ax.plot(x, d[f"ema{n}"].values, color=c, lw=1.1, label=f"EMA{n}")

    xr = len(d) + 15

    def hline(y, color, label, ls="-", lw=1.2):
        ax.axhline(y, color=color, ls=ls, lw=lw, alpha=0.85)
        ax.text(xr, y, f" {label} {y:.2f}", color=color, va="center", fontsize=8.5,
                fontweight="bold")

    for lv in levels_res:
        hline(lv["price"], dn_c, f"R({lv['touches']}x)")
    for lv in levels_sup:
        hline(lv["price"], up_c, f"S({lv['touches']}x)")
    for k, v in keys.items():
        if v is not None:
            hline(v, "#9598a1", k, ls="--", lw=0.9)

    if trade:
        col = up_c if trade["side"] == "BUY" else dn_c
        x0, bw = len(d) + 1, 13
        ax.add_patch(plt.Rectangle((x0, min(trade["entry"], trade["tp1"])), bw,
                                   abs(trade["tp1"] - trade["entry"]), color=up_c, alpha=0.18))
        ax.add_patch(plt.Rectangle((x0, min(trade["entry"], trade["sl"])), bw,
                                   abs(trade["sl"] - trade["entry"]), color=dn_c, alpha=0.18))
        for y, lab in ((trade["entry"], "ENTRY"), (trade["sl"], "SL"), (trade["tp1"], "TP1")):
            ax.plot([x0, x0 + bw], [y, y], color="white" if lab == "ENTRY" else col, lw=1.4)
            ax.text(x0 + bw / 2, y, f"{lab} {y:.2f}", color="white", ha="center",
                    va="center", fontsize=8.5,
                    bbox=dict(facecolor=col, edgecolor="none", alpha=0.8, pad=1.5))

    last = d["close"].iloc[-1]
    ax.axhline(last, color="#ffffff", lw=0.6, ls=":")
    step = max(1, len(d) // 10)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([t.strftime("%d %b %H:%M") for t in d.index[::step]],
                       color="#b2b5be", fontsize=8, rotation=0)
    ax.tick_params(colors="#b2b5be", labelsize=8)
    ax.grid(color=grid, lw=0.5)
    for s in ax.spines.values():
        s.set_color(grid)
    ax.set_xlim(-1, len(d) + 30)
    lo = min(d["low"].min(), *(l["price"] for l in levels_sup[:2]), *( [trade["sl"], trade["tp1"]] if trade else []))
    hi = max(d["high"].max(), *(l["price"] for l in levels_res[:2]), *( [trade["sl"], trade["tp1"]] if trade else []))
    pad = (hi - lo) * 0.05
    ax.set_ylim(lo - pad, hi + pad)
    ax.yaxis.tick_right()
    ax.legend(loc="upper left", facecolor=bg, edgecolor=grid, labelcolor="#b2b5be", fontsize=8)
    if trade:
        ax.text(0.01, 0.02, f"Top idea: {trade['side']} {trade['order']}  RR {trade['rr_tp1']}",
                transform=ax.transAxes, color="#d1d4dc", fontsize=9)
    ax.set_title(title, color="#d1d4dc", fontsize=12, loc="left")
    fig.tight_layout()
    fig.savefig(path, facecolor=bg)
    plt.close(fig)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="R_100")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    h1_all, _, fetched = load(args.data, args.symbol, "1h")
    h1_all = add_ind(h1_all)
    h1 = h1_all[h1_all["closed"]]           # structure from closed candles only
    price = float(h1_all["close"].iloc[-1])  # live price from forming candle
    a = float(h1["atr"].iloc[-1])
    last = h1.iloc[-1]

    h4, _, _ = load(args.data, args.symbol, "4h")
    h4 = add_ind(h4)
    h4c = h4[h4["closed"]]
    try:
        m15, _, _ = load(args.data, args.symbol, "15m")
        m15 = add_ind(m15)
    except FileNotFoundError:
        m15 = None

    sh, sl = swings(h1)
    st = structure(h1, sh, sl)
    sh4, sl4 = swings(h4c)
    st4 = structure(h4c, sh4, sl4)

    # zones from swings in the last ~10 days of 1H candles
    lookback = len(h1) - 240
    lv = cluster_levels([s for s in sh if s[0] >= lookback],
                        [s for s in sl if s[0] >= lookback], 0.4 * a, len(h1))
    sup = sorted([l for l in lv if l["price"] < price - 0.1 * a],
                 key=lambda l: price - l["price"])
    res = sorted([l for l in lv if l["price"] > price + 0.1 * a],
                 key=lambda l: l["price"] - price)
    # drop weak single-touch levels if stronger ones exist nearby
    def pick(cands):
        near = cands[:5]
        strong = [l for l in near if l["touches"] >= 2]
        return (strong + [l for l in near if l not in strong])[:3] if len(strong) < 3 else strong[:3]
    sup = sorted(pick(sup), key=lambda l: price - l["price"])
    res = sorted(pick(res), key=lambda l: l["price"] - price)

    day = h1_all.index[-1].normalize()
    today = h1_all[h1_all.index >= day]
    prev = h1_all[(h1_all.index >= day - pd.Timedelta(days=1)) & (h1_all.index < day)]
    week = h1_all[h1_all.index >= h1_all.index[-1] - pd.Timedelta(days=7)]
    keys = {
        "PDH": float(prev["high"].max()) if len(prev) else None,
        "PDL": float(prev["low"].min()) if len(prev) else None,
        "Today open": float(today["open"].iloc[0]) if len(today) else None,
    }
    key_levels = {**{k: round(v, 2) if v else None for k, v in keys.items()},
                  "Today high": round(float(today["high"].max()), 2),
                  "Today low": round(float(today["low"].min()), 2),
                  "7d high": round(float(week["high"].max()), 2),
                  "7d low": round(float(week["low"].min()), 2)}

    # volatility regime
    atr_med = float(h1["atr"].tail(200).median())
    atr_ratio = a / atr_med
    bbw_pct = float((h1["bbw"].tail(200) < h1["bbw"].iloc[-1]).mean() * 100)
    last_rng_atr = float((last.high - last.low) / a)
    regime = ("expansion" if atr_ratio > 1.25 else
              "compression" if atr_ratio < 0.8 or bbw_pct < 20 else "normal")
    rng24 = float(h1["high"].tail(24).max() - h1["low"].tail(24).min())
    avg_rng24 = float((h1["high"].rolling(24).max() - h1["low"].rolling(24).min()).tail(240).mean())

    # bias score
    score = 0
    score += 1 if "uptrend" in st["trend"] else -1 if "downtrend" in st["trend"] else 0
    score += 1 if last.close > last.ema50 else -1
    score += 1 if last.ema20 > last.ema50 else -1
    l4 = h4c.iloc[-1]
    score += 1 if l4.close > l4.ema50 else -1
    score += 1 if l4.ema20 > l4.ema50 else -1
    bias = "bullish" if score >= 3 else "bearish" if score <= -3 else "neutral"

    liq = equal_hl(sh, sl, 0.15 * a, h1)
    div = divergence(h1, sh, sl)
    pats = candle_pattern(h1)

    watch = []
    if regime == "compression":
        watch.append("Volatility is compressed (ATR/BB width low): expect an expansion move, breakouts more likely than fades")
    if regime == "expansion":
        watch.append("Volatility is expanded: wider stops needed, fading moves is riskier, expect some mean reversion after big candles")
    if last_rng_atr > 1.8:
        watch.append(f"Last 1H candle was {last_rng_atr:.1f}x ATR: chasing it is risky, wait for a pullback")
    for q in liq:
        side = "above" if q["price"] > price else "below"
        watch.append(f"{q['type']} at {q['price']:.2f} ({side} price): resting liquidity, likely sweep target")
    if div:
        watch.append(div)
    r_now = float(last.rsi)
    if r_now > 70:
        watch.append(f"RSI {r_now:.0f} overbought on 1H")
    elif r_now < 30:
        watch.append(f"RSI {r_now:.0f} oversold on 1H")
    for nm in ("PDH", "PDL"):
        v = keys[nm]
        if v and abs(price - v) < 0.7 * a:
            watch.append(f"Price is close to {nm} {v:.2f}: watch for a sweep and reject or a clean break")
    if sup and price - sup[0]["price"] < 0.5 * a:
        watch.append(f"Sitting right on support {sup[0]['price']:.2f}")
    if res and res[0]["price"] - price < 0.5 * a:
        watch.append(f"Sitting right under resistance {res[0]['price']:.2f}")
    h1_up = last.close > last.ema50
    h4_up = l4.close > l4.ema50
    if h1_up != h4_up:
        watch.append("1H and 4H disagree on direction: lower conviction, size down")
    if pats:
        watch.append("Last closed 1H candle: " + ", ".join(pats))

    trades = build_trades(price, a, bias, sup, res)

    m15_info = None
    if m15 is not None:
        lm = m15.iloc[-1]
        m15_info = {"close_vs_ema20": "above" if lm.close > lm.ema20 else "below",
                    "rsi": round(float(lm.rsi), 1)}

    name = NAMES.get(args.symbol, args.symbol)
    now = datetime.fromtimestamp(fetched, tz=timezone.utc)
    result = {
        "symbol": args.symbol, "name": name, "timeframe": "1H",
        "data_time_utc": now.isoformat(),
        "last_closed_candle_utc": h1.index[-1].isoformat(),
        "data_age_minutes": None,
        "price": round(price, 2),
        "bias": bias, "bias_score_-5_to_5": score,
        "structure_1h": st, "structure_4h": {"trend": st4["trend"], "last_bos": st4["last_bos"]},
        "indicators_1h": {
            "ema20": round(float(last.ema20), 2), "ema50": round(float(last.ema50), 2),
            "ema200": round(float(last.ema200), 2), "rsi14": round(r_now, 1),
            "atr14": round(a, 2), "atr_pct_of_price": round(a / price * 100, 3),
        },
        "m15": m15_info,
        "volatility": {
            "regime": regime, "atr_vs_200bar_median": round(atr_ratio, 2),
            "bb_width_percentile": round(bbw_pct, 0),
            "last_candle_range_x_atr": round(last_rng_atr, 2),
            "last_24h_range": round(rng24, 2), "avg_24h_range": round(avg_rng24, 2),
        },
        "resistance": res, "support": sup, "key_levels": key_levels,
        "liquidity": liq, "divergence": div, "last_candle_patterns": pats,
        "watch_out": watch, "trade_candidates": trades,
        "chart": str(out / "chart.png"),
    }
    result["data_age_minutes"] = round(
        (datetime.now(timezone.utc) - now).total_seconds() / 60, 1)

    best = trades[0] if trades else None
    draw_chart(h1_all, sup, res, keys, best,
               f"{name} ({args.symbol})  1H   price {price:.2f}   bias {bias.upper()}   "
               f"{now.strftime('%d %b %Y %H:%M UTC')}",
               out / "chart.png")
    (out / "analysis.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
