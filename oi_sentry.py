#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OI Sentry: Binance vs Bybit "Seesaw" Detector (BTCUSDT Perps)
"""
import argparse
import json
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, List

import requests

BINANCE_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BYBIT_URL   = "https://api.bybit.com/v5/market/open-interest"

STATE_FILE = "oi_state.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_binance_oi(symbol: str = "BTCUSDT") -> Optional[float]:
    try:
        r = requests.get(BINANCE_URL, params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["openInterest"])
    except Exception as e:
        print(f"[{utc_now_iso()}] Binance OI fetch error: {e}", file=sys.stderr)
        return None


def fetch_bybit_oi(symbol: str = "BTCUSDT") -> Optional[float]:
    try:
        params = {"category": "linear", "symbol": symbol, "interval": "5min", "limit": 1}
        r = requests.get(BYBIT_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        lst = data.get("result", {}).get("list", [])
        if not lst:
            raise ValueError("empty result.list")
        latest = lst[-1]
        return float(latest["openInterest"])
    except Exception as e:
        print(f"[{utc_now_iso()}] Bybit OI fetch error: {e}", file=sys.stderr)
        return None


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def load_state(path: str = STATE_FILE) -> Dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict, path: str = STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def push_discord(webhook: str, content: str, embeds: Optional[List[Dict]] = None) -> bool:
    if not webhook:
        return False
    try:
        payload = {"content": content}
        if embeds:
            payload["embeds"] = embeds
        r = requests.post(webhook, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[{utc_now_iso()}] Discord post error: {e}", file=sys.stderr)
        return False


def rolling_stats(values: List[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, 1e-9
    m = sum(values) / len(values)
    var = sum((v - m) ** 2 for v in values) / max(1, len(values)-1)
    return m, math.sqrt(max(var, 1e-9))


def zscore(x: float, mean: float, std: float) -> float:
    if std <= 1e-9:
        return 0.0
    return (x - mean) / std


def fmt(v: float) -> str:
    return f"{v:,.2f}"


def run_loop(args):
    state = load_state(args.state_file)
    history = state.get("history", [])
    _iters = 0

    def sigint_handler(signum, frame):
        print(f"\n[{utc_now_iso()}] Stopping...")
        state["history"] = history[-args.keep_points:]
        save_state(state, args.state_file)
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    last_binance = history[-1]["oi_binance"] if history else None
    last_bybit   = history[-1]["oi_bybit"] if history else None

    while True:
        ts = utc_now_iso()
        oi_binance = fetch_binance_oi("BTCUSDT")
        oi_bybit   = fetch_bybit_oi("BTCUSDT")

        if oi_binance is None or oi_bybit is None:
            if args.verbose:
                print(f"[{ts}] Skipped due to fetch failure (binance={oi_binance}, bybit={oi_bybit})")
            time.sleep(args.poll_sec)
            _iters += 1
            if args.max_iter and _iters >= args.max_iter:
                state["history"] = history[-args.keep_points:]
                save_state(state, args.state_file)
                return
            continue

        history.append({"ts": ts, "oi_binance": oi_binance, "oi_bybit": oi_bybit})
        history[:] = history[-args.keep_points:]

        if last_binance is None or last_bybit is None:
            if args.verbose:
                print(f"[{ts}] Warmup: binance={fmt(oi_binance)}, bybit={fmt(oi_bybit)}")
            last_binance, last_bybit = oi_binance, oi_bybit
            state["history"] = history
            save_state(state, args.state_file)
            time.sleep(args.poll_sec)
            _iters += 1
            if args.max_iter and _iters >= args.max_iter:
                return
            continue

        d_binance_pct = pct_change(oi_binance, last_binance)
        d_bybit_pct   = pct_change(oi_bybit, last_bybit)

        bins = [h["oi_binance"] for h in history]
        bybs = [h["oi_bybit"] for h in history]
        m_b, s_b = rolling_stats(bins)
        m_y, s_y = rolling_stats(bybs)
        z_b = zscore(oi_binance, m_b, s_b)
        z_y = zscore(oi_bybit, m_y, s_y)

        total_prev = last_binance + last_bybit
        total_now  = oi_binance + oi_bybit
        total_swing = abs(total_now - total_prev)

        if args.verbose:
            print(f"[{ts}] BIN: {fmt(oi_binance)} ({d_binance_pct:+.2f}%), z={z_b:+.2f} | "
                  f"BYB: {fmt(oi_bybit)} ({d_bybit_pct:+.2f}%), z={z_y:+.2f} | "
                  f"ΔTotal={fmt(total_swing)}")

        alerts = []

        seesaw_up = (d_binance_pct >= args.up_thresh_pct and d_bybit_pct <= -args.down_thresh_pct)
        seesaw_dn = (d_bybit_pct   >= args.up_thresh_pct and d_binance_pct <= -args.down_thresh_pct)
        if seesaw_up or seesaw_dn:
            side = "Binance↑ / Bybit↓" if seesaw_up else "Bybit↑ / Binance↓"
            alerts.append(("🟨 Seesaw", side))

        if d_binance_pct >= args.sync_thresh_pct and d_bybit_pct >= args.sync_thresh_pct:
            alerts.append(("🟩 Sync Pump", "Both OI increasing"))

        if d_binance_pct <= -args.sync_thresh_pct and d_bybit_pct <= -args.sync_thresh_pct:
            alerts.append(("🟥 Sync Flush", "Both OI decreasing"))

        if total_swing >= args.total_swing_thresh:
            alerts.append(("🟦 Total Swing", f"|Δ(B+Y)| ≥ {fmt(args.total_swing_thresh)}"))

        if alerts and args.webhook:
            title = " / ".join(a[0] for a in alerts)
            lines = [
                f"**{title}**  `{ts}`",
                f"Binance OI: **{fmt(oi_binance)}**  ({d_binance_pct:+.2f}%)  z={z_b:+.2f}",
                f"Bybit   OI: **{fmt(oi_bybit)}**  ({d_bybit_pct:+.2f}%)  z={z_y:+.2f}",
                f"ΔTotal OI: {fmt(total_swing)} (contracts)",
                f"Details: " + " | ".join(f"{a[0]} → {a[1]}" for a in alerts),
            ]
            content = "\n".join(lines)
            push_discord(args.webhook, content)

        last_binance, last_bybit = oi_binance, oi_bybit
        state["history"] = history
        save_state(state, args.state_file)

        time.sleep(args.poll_sec)
        _iters += 1
        if args.max_iter and _iters >= args.max_iter:
            state["history"] = history[-args.keep_points:]
            save_state(state, args.state_file)
            return


def parse_args():
    ap = argparse.ArgumentParser(description="Binance vs Bybit OI 'Seesaw' Detector")
    ap.add_argument("--webhook", type=str, default=os.getenv("DISCORD_WEBHOOK", ""),
                    help="Discord webhook URL (or set DISCORD_WEBHOOK env)")
    ap.add_argument("--poll-sec", type=int, default=60, help="Polling interval seconds")
    ap.add_argument("--keep-points", type=int, default=360, help="History buffer length (points)")
    ap.add_argument("--up-thresh-pct", type=float, default=0.5,
                    help="Seesaw: 'up' threshold in % for one side (default 0.5)")
    ap.add_argument("--down-thresh-pct", type=float, default=0.5,
                    help="Seesaw: 'down' threshold in % for the other side (default 0.5)")
    ap.add_argument("--sync-thresh-pct", type=float, default=0.5,
                    help="Sync Pump/Flush threshold in % for both sides (default 0.5)")
    ap.add_argument("--total-swing-thresh", type=float, default=2_000_000,
                    help="Absolute contracts swing threshold across both exchanges per tick")
    ap.add_argument("--state-file", type=str, default=STATE_FILE, help="Path to state file")
    ap.add_argument("--verbose", action="store_true", help="Print debug logs")
    ap.add_argument("--max-iter", type=int, default=0,
                    help="If >0, run at most this many iterations (good for CI)")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_loop(args)
