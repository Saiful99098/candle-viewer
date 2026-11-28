#!/usr/bin/env python3
"""View BLUE FISH PRIME candles from the Render Postgres DB, in a browser.

Two ways to run it — it only *reads* the DB, never writes anything,
so your bot keeps running untouched.

LOCAL (your PC only):
    1) Render Dashboard -> littlematrix-db -> Info
       -> copy the EXTERNAL Database URL
    2) Git Bash:  export DATABASE_URL="postgres://..."
       PowerShell: $env:DATABASE_URL="postgres://..."
    3) python view_candles.py
    4) Open http://127.0.0.1:8001/

ONLINE (Render, so you can open it from your phone anywhere):
    Push this folder to GitHub, then in Render: New -> Web Service
    -> pick the repo. Set the DATABASE_URL environment variable to
    the littlematrix-db EXTERNAL Database URL and deploy.
    Your viewer is then at https://<service-name>.onrender.com/
    (Render sets PORT and the script listens on 0.0.0.0 by itself.)

Never paste the DATABASE_URL in chat and never commit it to GitHub.
The viewer shows only candles (market data) — no password is ever shown.

Demo (no DB needed, fake sample candles, just to see the page):
    python view_candles.py --demo

Password protection (optional): set the VIEWER_PASSWORD environment
variable in Render to any password you choose (never put it in the
code or in GitHub). When it is set, every page and API request asks
for HTTP Basic Auth (any username, that password). When it is unset,
the viewer stays open exactly as before.

UI: dark-ash premium theme, asset search, auto-refresh every 30s
for today, live/stale badge, gap detector, clean chart (candles only,
no indicators), compare mode, downloads, live forming candle with a
countdown in the chart header, payouts, signal generator and a bot
status bar. Threaded server + DB reconnect so one slow request can
never stall the page or Render's health check.
"""
from __future__ import annotations

import argparse
import base64
import functools
import hmac
import html
import json
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
    DHAKA = ZoneInfo("Asia/Dhaka")
except Exception:  # pragma: no cover
    DHAKA = timezone(timedelta(hours=6))
UTC = timezone.utc

DEFAULT_PORT = 8001
LIVE_STALE_SECONDS = 10  # no tick for this long = STALE (same as the bot's live badge rule)

# --- concurrency + DB health (fix, 2026-11-28): the viewer used to run
# on a single-threaded HTTPServer, so one slow request (best_times /
# signals) blocked every other request — including Render's health
# check, which then marked the service failed and the in-flight
# requests got 502s. Now it threads, and all Store DB access is
# serialized through one re-entrant lock (psycopg2 connections are
# not safe for concurrent use). A dead connection is re-established
# once and the query retried, instead of failing.
try:
    import psycopg2 as _psycopg2
    _CONN_ERRORS = (_psycopg2.InterfaceError, _psycopg2.OperationalError)
except Exception:  # pragma: no cover - demo without psycopg2
    _psycopg2 = None
    _CONN_ERRORS = ()

STORE_LOCK = threading.RLock()


def _is_conn_error(exc):
    """True for a dead/broken DB connection (InterfaceError /
    OperationalError) — the only errors worth reconnecting for. A
    missing table or bad SQL is a real error, not a dead connection."""
    return bool(_CONN_ERRORS) and isinstance(exc, _CONN_ERRORS)


def _db_retry(fn):
    """Retry a Store method once after reconnecting, but only when
    the failure is a dead connection. Other errors pass through."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception as e:
            if not _is_conn_error(e) or self.demo:
                raise
            self._rollback()
            self._reconnect()
            return fn(self, *a, **kw)
    return wrapper


def _locked(fn):
    """Serialize Store DB access through one re-entrant lock (RLock:
    best_times/signals call self.candles internally, so the same
    thread must be able to re-enter)."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with STORE_LOCK:
            return fn(self, *a, **kw)
    return wrapper

# --- signal-engine constants, mirrored from bot/signal_engine.py ---
MIN_BODY = 0.00025
MIN_BODY_RATIO = 0.5
MIN_BODY_REL = 0.0000125
MIN_GAP_MINUTES = 7


def normalize_asset(asset):
    """Same rule as the bot: BTCUSD-OTC -> BTCUSD_otc."""
    a = (asset or "").strip().upper().replace("-", "_")
    if a.endswith("_OTC"):
        a = a[:-4] + "_otc"
    return a


def display_asset(asset):
    """Internal 'USDMXN_otc' -> display 'USDMXN-OTC' (bot rule)."""
    a = (asset or "").upper().strip()
    if a.endswith("_OTC"):
        a = a[:-4] + "-OTC"
    return a


def dhaka_date_range_utc(date_str):
    """Full Dhaka day -> half-open UTC range [start, end)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, tzinfo=DHAKA).astimezone(UTC)
    return start, start + timedelta(days=1)


def today_dhaka_str():
    return datetime.now(DHAKA).strftime("%Y-%m-%d")


def _parse_ts(ts):
    """A DB ts value (datetime or UTC 'YYYY-MM-DD HH:MM:SS') -> aware UTC datetime."""
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _utc_iso_to_dhaka(value):
    """Stored UTC 'YYYY-MM-DD HH:MM:SS' -> Dhaka string, or None."""
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return dt.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_tick_ts(value):
    """A live-tick ts: unix seconds (int/float/numeric str) or a UTC
    ISO string -> epoch seconds (int), or None when unreadable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=UTC).timestamp())
        except ValueError:
            continue
    return None


def _fnum(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def hhmm_to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ------------------------------------------------- signal math (bot mirror) ----
def majority_vote(directions):
    """Port of bot/signal_engine.py majority_vote (DOJI ignored,
    tie -> direction None). Returns (direction, win_rate, calls, puts)."""
    calls = sum(1 for d in directions if d == "CALL")
    puts = sum(1 for d in directions if d == "PUT")
    total = calls + puts
    if total == 0:
        return None, 0.0, calls, puts
    if calls > puts:
        return "CALL", calls / total, calls, puts
    if puts > calls:
        return "PUT", puts / total, calls, puts
    return None, 0.5, calls, puts  # tie


def _is_strong(candle, min_body=MIN_BODY, min_ratio=MIN_BODY_RATIO):
    body = abs(candle["close"] - candle["open"])
    rng = candle["high"] - candle["low"]
    if rng <= 0:
        return False
    return body >= min_body and (body / rng) >= min_ratio


def _is_strong_adaptive(candle):
    price = abs(candle["close"]) or 1.0
    return _is_strong(candle, min_body=price * MIN_BODY_REL)


def _apply_strength_cascade(signals):
    """Port of bot _apply_strength_cascade: strict first, then
    price-scaled adaptive, then ratio-only 0.4, ratio-only 0.3,
    then strongest by body/range ratio."""
    strict = [s for s in signals if _is_strong(s["_candle"])]
    if strict:
        return strict
    adaptive = [s for s in signals if _is_strong_adaptive(s["_candle"])]
    if adaptive:
        return adaptive

    def _ratio_only(min_ratio):
        out = []
        for s in signals:
            c = s["_candle"]
            rng = c["high"] - c["low"]
            if rng > 0 and abs(c["close"] - c["open"]) / rng >= min_ratio:
                out.append(s)
        return out

    relaxed = _ratio_only(0.4)
    if relaxed:
        return relaxed
    relaxed = _ratio_only(0.3)
    if relaxed:
        return relaxed

    def _ratio(c):
        rng = c["high"] - c["low"]
        return abs(c["close"] - c["open"]) / rng if rng > 0 else 0.0

    return sorted(signals, key=lambda s: -_ratio(s["_candle"]))


def _apply_gap_filter(signals, min_gap=MIN_GAP_MINUTES):
    """Port of bot apply_gap_filter: keep signals >= min_gap apart."""
    kept = []
    last_min = -10 ** 9
    for s in sorted(signals, key=lambda x: x["hhmm"]):
        mins = hhmm_to_minutes(s["hhmm"])
        if mins - last_min >= min_gap:
            kept.append(s)
            last_min = mins
    return kept


def _annotate_strength(signals):
    for s in signals:
        c = s.get("_candle")
        if c:
            rng = c["high"] - c["low"]
            s["strength"] = (abs(c["close"] - c["open"]) / rng) if rng > 0 else 0.0
        else:
            s["strength"] = 0.0
        s.pop("_candle", None)
    return signals


# ---------------------------------------------------------------- DB ----
def connect_real():
    """Postgres via the Render *External* DATABASE_URL (read-only use)."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL is not set.")
        print("  Render Dashboard -> littlematrix-db -> Info")
        print("  -> copy the EXTERNAL Database URL, then in Git Bash:")
        print('  export DATABASE_URL="postgres://..."')
        print("  then run: python view_candles.py")
        print("  (Never paste the URL in chat or commit it to GitHub.)")
        sys.exit(2)
    if not url.startswith(("postgres://", "postgresql://")):
        print("ERROR: DATABASE_URL does not look like a Postgres URL.")
        print("Use the EXTERNAL Database URL from Render, not the Internal one.")
        sys.exit(2)
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary")
        sys.exit(2)
    return psycopg2.connect(url)


def connect_demo():
    """Fake in-memory SQLite DB with sample candles — UI test only.

    Also seeds the bot's payouts + collector_state tables (same shape
    as the real DB) so the new endpoints return sane demo JSON."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE candles (asset TEXT, ts TIMESTAMP, open REAL, "
        "high REAL, low REAL, close REAL, UNIQUE(asset, ts))"
    )
    conn.execute(
        "CREATE TABLE payouts (asset TEXT PRIMARY KEY, payout_1m REAL, "
        "is_open INTEGER NOT NULL DEFAULT 1, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE collector_state (key TEXT PRIMARY KEY, value TEXT)"
    )
    base = datetime(2026, 11, 26, 0, 0, tzinfo=DHAKA).astimezone(UTC)
    price = 170.0
    for asset in ("BTCUSD_otc", "USDMXN_otc"):
        p = price if asset.startswith("BTC") else 17.0
        for i in range(120):
            ts = base + timedelta(minutes=i)
            o, c = p, p + (0.02 if i % 3 else -0.01)
            conn.execute(
                "INSERT INTO candles VALUES (?,?,?,?,?,?)",
                (asset, ts.strftime("%Y-%m-%d %H:%M:%S"),
                 o, max(o, c) + 0.01, min(o, c) - 0.01, c),
            )
            p = c
    now_utc = datetime.now(UTC)
    conn.execute(
        "INSERT INTO payouts VALUES (?,?,?,?)",
        ("BTCUSD_otc", 93.0, 1, now_utc.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.execute(
        "INSERT INTO payouts VALUES (?,?,?,?)",
        ("USDMXN_otc", 91.0, 1, now_utc.strftime("%Y-%m-%d %H:%M:%S")),
    )
    flow = json.dumps({"stopped": [["EURUSD_otc", "11:13"]],
                       "resumed": [["BTCUSD_otc", "11:20"]]})
    state = {
        "last_collection": (now_utc - timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S"),
        "next_collection": (now_utc + timedelta(minutes=18)).strftime("%Y-%m-%d %H:%M:%S"),
        "last_backfill": (now_utc - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "quotex_last_login": (now_utc - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "quotex_session": "connected",
        "live_stream_status": "connected",
        "live_stream_last_seen": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "live_stream_flow": flow,
        "bot_on_free": "1",
        "bot_on_premium": "1",
    }
    for k, v in state.items():
        conn.execute("INSERT INTO collector_state VALUES (?,?)", (k, v))
    conn.commit()
    return conn


class Store:
    """One shared DB connection. READ-ONLY: every query here is a
    SELECT (the demo seeder above is the only INSERT, and it writes
    to a throwaway in-memory SQLite DB, never to the real candles)."""

    def __init__(self, conn, demo=False):
        self.conn = conn
        self.demo = demo
        self.ph = "?" if demo else "%s"

    def _rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def _reconnect(self):
        """Drop the dead connection and open a fresh one (real mode
        only — demo mode has an in-memory DB that cannot be rebuilt).
        Called only from _db_retry, while STORE_LOCK is held."""
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = connect_real()

    @_locked
    @_db_retry
    def assets(self):
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT asset FROM candles ORDER BY asset")
        return [r[0] for r in cur.fetchall()]

    @_locked
    @_db_retry
    def candles(self, asset, date_str):
        asset = normalize_asset(asset)
        start, end = dhaka_date_range_utc(date_str)
        fmt = "%Y-%m-%d %H:%M:%S"
        cur = self.conn.cursor()
        cur.execute(
            "SELECT ts, open, high, low, close FROM candles "
            f"WHERE asset = {self.ph} AND ts >= {self.ph} AND ts < {self.ph} "
            "ORDER BY ts",
            (asset, start.strftime(fmt), end.strftime(fmt)),
        )
        rows = []
        for ts, o, h, l, c in cur.fetchall():
            dt = _parse_ts(ts)
            rows.append({
                "time": dt.astimezone(DHAKA).strftime("%H:%M"),
                "open": float(o), "high": float(h),
                "low": float(l), "close": float(c),
            })
        return asset, rows

    @_locked
    @_db_retry
    def dates(self, asset=None):
        """Dhaka dates that have candles (for one asset, or all)."""
        try:
            cur = self.conn.cursor()
            if asset:
                cur.execute(
                    f"SELECT ts FROM candles WHERE asset = {self.ph} ORDER BY ts",
                    (normalize_asset(asset),))
            else:
                cur.execute("SELECT ts FROM candles ORDER BY ts")
            out = set()
            for (ts,) in cur.fetchall():
                out.add(_parse_ts(ts).astimezone(DHAKA).strftime("%Y-%m-%d"))
            return sorted(out)
        except Exception as e:
            if _is_conn_error(e):
                self._rollback()
                raise
            self._rollback()
            return []

    @_locked
    @_db_retry
    def get_state(self, key, default=None):
        """One collector_state value (bot's DB state keys). None when
        the table/key does not exist yet — never raises."""
        try:
            cur = self.conn.cursor()
            cur.execute(
                f"SELECT value FROM collector_state WHERE key = {self.ph}",
                (key,))
            row = cur.fetchone()
            return row[0] if row else default
        except Exception as e:
            if _is_conn_error(e):
                self._rollback()
                raise
            self._rollback()
            return default

    @_locked
    @_db_retry
    def payouts(self):
        """Bot's stored payouts (candles_db.get_payouts): the payouts
        table, best payout first. [] when the collector never stored
        a snapshot yet (or the table is missing)."""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT asset, payout_1m, is_open, updated_at FROM payouts")
            rows = [{"asset": a,
                     "payout": float(p) if p is not None else None,
                     "open": bool(o), "updated_at": u}
                    for a, p, o, u in cur.fetchall()]
            rows.sort(key=lambda r: (-(r["payout"] if r["payout"] is not None else -1),
                                     r["asset"]))
            return rows
        except Exception as e:
            if _is_conn_error(e):
                self._rollback()
                raise
            self._rollback()
            return []

    def _demo_live_tick(self, asset):
        """Demo only: a fresh rich-form forming candle every call, so
        the live candle + countdown can be seen without a real bot.
        (Real mode reads the bot's live_ticks DB state key instead.)"""
        dates = self.dates(asset)
        if not dates:
            return {"asset": asset, "price": None, "ts": None, "stale": True}
        _, rows = self.candles(asset, dates[-1])
        if not rows:
            return {"asset": asset, "price": None, "ts": None, "stale": True}
        now = datetime.now(DHAKA)
        base = rows[-1]["close"]
        price = base + math.sin(time.time() / 7.0) * 0.001 * base
        return {"asset": asset, "price": round(price, 5),
                "ts": int(time.time()), "stale": False,
                "minute": now.strftime("%Y-%m-%d %H:%M"),
                "open": base, "high": max(base, price),
                "low": min(base, price), "close": price, "demo": True}

    @_locked
    @_db_retry
    def live_tick(self, asset):
        """The bot's live tick for one asset, from the collector_state
        key 'live_ticks' (published by the bot's live stream).

        Two publisher forms are handled (the bot publisher is still
        being built, so either may arrive):
          simple: {asset: {"price": float, "ts": int}}
          rich:   {asset: {"minute": "YYYY-MM-DD HH:MM", "open": ...,
                           "high": ..., "low": ..., "close": ...,
                           "ts": int}}
        Returns {"asset", "price", "ts", "stale", "minute", "open",
        "high", "low", "close"} — price null + stale true when the key
        or the asset is missing (the viewer keeps working, the live
        candle is simply hidden). Never raises."""
        asset = normalize_asset(asset)
        if self.demo:
            return self._demo_live_tick(asset)
        raw = self.get_state("live_ticks")
        empty = {"asset": asset, "price": None, "ts": None, "stale": True,
                 "minute": None, "open": None, "high": None,
                 "low": None, "close": None}
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except Exception:
            return empty
        entry = None
        if isinstance(data, dict):
            for key in (asset, asset.upper(),
                        asset.replace("_otc", "-OTC")):
                if isinstance(data.get(key), dict):
                    entry = data[key]
                    break
        if entry is None:
            return empty
        price = _fnum(entry.get("price", entry.get("close")))
        ts = _parse_tick_ts(entry.get("ts"))
        stale = True if ts is None else (time.time() - ts > LIVE_STALE_SECONDS)
        return {"asset": asset, "price": price, "ts": ts, "stale": stale,
                "minute": entry.get("minute"),
                "open": _fnum(entry.get("open")),
                "high": _fnum(entry.get("high")),
                "low": _fnum(entry.get("low")),
                "close": _fnum(entry.get("close"))}

    @_locked
    @_db_retry
    def bot_status(self):
        """Bot Status from the bot's DB state keys — the same keys
        handlers._run_bot_status reads via collector.get_status_summary
        (last/next collection, Quotex session/login, live stream
        status + last seen) plus live_stream._FLOW_STATE_KEY
        ('live_stream_flow': stopped/resumed assets) and the
        bot_on_free / bot_on_premium toggles. Times are Dhaka."""
        stopped, resumed = {}, []
        raw_flow = self.get_state("live_stream_flow")
        if raw_flow:
            try:
                data = json.loads(raw_flow)
                stopped = {a: t for a, t in data.get("stopped", [])}
                resumed = [[a, t] for a, t in data.get("resumed", [])]
            except Exception:
                stopped, resumed = {}, []
        today = today_dhaka_str()
        today_count = None
        total = None
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM candles")
            total = cur.fetchone()[0]
            start, _ = dhaka_date_range_utc(today)
            fmt = "%Y-%m-%d %H:%M:%S"
            cur.execute(
                f"SELECT COUNT(*) FROM candles WHERE ts >= {self.ph} "
                f"AND ts < {self.ph}",
                (start.strftime(fmt),
                 datetime.now(UTC).strftime(fmt)))
            today_count = cur.fetchone()[0]
        except Exception as e:
            if _is_conn_error(e):
                self._rollback()
                raise
            self._rollback()
        return {
            "last_collection": _utc_iso_to_dhaka(self.get_state("last_collection")),
            "next_collection": _utc_iso_to_dhaka(self.get_state("next_collection")),
            "last_backfill": _utc_iso_to_dhaka(self.get_state("last_backfill")),
            "quotex_last_login": _utc_iso_to_dhaka(self.get_state("quotex_last_login")),
            "quotex_session": self.get_state("quotex_session"),
            "live_stream_status": self.get_state("live_stream_status"),
            "live_stream_last_seen": _utc_iso_to_dhaka(
                self.get_state("live_stream_last_seen")),
            "bot_on_free": self.get_state("bot_on_free", "1") != "0",
            "bot_on_premium": self.get_state("bot_on_premium", "1") != "0",
            "stopped": stopped,
            "resumed": resumed,
            "total_candles": total,
            "today_date": today,
            "today_count": today_count,
            "now_dhaka": datetime.now(DHAKA).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _vote_days(self, asset, ref_date, days):
        """The `days` most recent COMPLETED dates before ref_date with
        data for the asset (same idea as the bot's
        available_signal_dates). Falls back to dates <= ref_date when
        there is no completed day yet (first run / demo)."""
        all_dates = self.dates(asset)
        completed = [d for d in all_dates if d < ref_date]
        pool = completed if completed else [d for d in all_dates if d <= ref_date]
        return pool[-days:]

    def _group_by_hhmm(self, asset, use_dates):
        """{hhmm: {"dirs": [CALL/PUT...], "rep": last day's candle}}."""
        by_minute = {}
        for d in use_dates:
            _, rows = self.candles(asset, d)
            for r in rows:
                slot = by_minute.setdefault(
                    r["time"], {"dirs": [], "rep": r})
                if r["close"] > r["open"]:
                    slot["dirs"].append("CALL")
                elif r["close"] < r["open"]:
                    slot["dirs"].append("PUT")
                slot["rep"] = r  # representative = latest day's candle
        return by_minute

    @_locked
    @_db_retry
    def best_times(self, asset, days=5, ref_date=None):
        """Per-HH:MM majority-vote win rate over the past `days`
        completed days (same logic as the bot's signal engine:
        group by HH:MM across days, majority direction, win% =
        majority/total). No strength filter here — the chart marks
        every slot with win% >= 70%."""
        asset = normalize_asset(asset)
        ref_date = ref_date or today_dhaka_str()
        use = self._vote_days(asset, ref_date, days)
        by_minute = self._group_by_hhmm(asset, use)
        slots = []
        for hhmm in sorted(by_minute):
            slot = by_minute[hhmm]
            direction, win_rate, calls, puts = majority_vote(slot["dirs"])
            if direction is None:
                continue
            slots.append({"hhmm": hhmm, "direction": direction,
                          "win_rate": round(win_rate, 4),
                          "win_pct": round(win_rate * 100, 1),
                          "calls": calls, "puts": puts,
                          "votes": calls + puts})
        slots.sort(key=lambda s: (-s["win_rate"], s["hhmm"]))
        return {"asset": asset, "ref_date": ref_date, "days": len(use),
                "dates": use, "count": len(slots), "slots": slots}

    @_locked
    @_db_retry
    def signals(self, asset, date_str, days=5, min_win_pct=70, apply_gap=True):
        """Futures signals for `date_str`, built from the `days`
        completed days before it — a simplified port of the bot's
        generate_futures_signals_from_db: majority vote per HH:MM,
        win-rate filter (multi-day only), the bot's strength cascade,
        and the >= 7 min gap filter when apply_gap is True (default:
        fewer, higher-accuracy signals, as the owner wants)."""
        asset = normalize_asset(asset)
        use = self._vote_days(asset, date_str, days)
        n_days = len(use)
        by_minute = self._group_by_hhmm(asset, use)
        sigs = []
        for hhmm in sorted(by_minute):
            slot = by_minute[hhmm]
            direction, win_rate, calls, puts = majority_vote(slot["dirs"])
            if direction is None:
                continue
            if n_days >= 2 and win_rate < min_win_pct / 100.0:
                continue
            sigs.append({"asset": asset, "hhmm": hhmm,
                         "direction": direction, "win_rate": round(win_rate, 4),
                         "win_pct": round(win_rate * 100, 1),
                         "calls": calls, "puts": puts,
                         "days": n_days, "_candle": slot["rep"]})
        sigs = _apply_strength_cascade(sigs)
        if apply_gap:
            sigs = _apply_gap_filter(sigs)
        sigs = _annotate_strength(sigs)
        for s in sigs:
            s["line"] = (f"{display_asset(asset)},{s['hhmm']} M1 "
                         f"{s['direction']}")
            s["strength"] = round(s["strength"], 3)
        return {"asset": asset, "date": date_str, "days": n_days,
                "dates": use, "threshold_pct": min_win_pct,
                "gap_applied": bool(apply_gap),
                "count": len(sigs), "signals": sigs}


STORE = None  # set in main()


# -------------------------------------------------------------- HTML ----
def fmt_price(v):
    """Short readable price: big prices get 2 decimals, small ones 5."""
    v = float(v)
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 100:
        return f"{v:.3f}"
    return f"{v:.5f}"


def dir_pill(r):
    if r["close"] > r["open"]:
        return '<span class="pill call">CALL</span>'
    if r["close"] < r["open"]:
        return '<span class="pill put">PUT</span>'
    return '<span class="pill doji">DOJI</span>'

CSS = """
:root{
  --bg:#141417; --panel:#1f1f24; --panel2:#2a2a31; --border:#383842;
  --text:#edeef2; --muted:#9a9ca8;
  --violet:#8b5cf6; --cyan:#06b6d4; --green:#22c55e; --red:#f43f5e; --amber:#f59e0b;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,'Segoe UI',sans-serif}
.topbar{height:4px;background:linear-gradient(90deg,var(--violet),var(--cyan),var(--green))}
.wrap{max-width:1000px;margin:0 auto;padding:16px 14px 40px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:14px 16px;margin-bottom:12px}
.hero{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
  border:1px solid transparent;
  background:linear-gradient(var(--panel),var(--panel)) padding-box,
    linear-gradient(135deg,var(--violet),var(--cyan),var(--green)) border-box}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  font-weight:800;font-size:17px;color:#fff;background:linear-gradient(135deg,var(--violet),var(--cyan))}
.brand h1{margin:0;font-size:20px;letter-spacing:1px}
.brand h1 span{background:linear-gradient(90deg,var(--violet),var(--cyan));-webkit-background-clip:text;background-clip:text;color:transparent}
.brand p{margin:2px 0 0;color:var(--muted);font-size:12px;letter-spacing:2px}
.hero-right{display:flex;align-items:center;gap:10px}
.clock{font-family:ui-monospace,Menlo,monospace;font-size:15px;color:var(--text)}
.auto-pill{padding:4px 10px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:1px}
.auto-pill.on{background:rgba(34,197,94,.18);color:#4ade80}
.auto-pill.off{background:rgba(255,255,255,.08);color:var(--muted)}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.search{position:relative;flex:1;min-width:220px}
.search input,.controls input[type=date],.sig-form input,.sig-form select{width:100%;background:#17171b;border:1px solid var(--border);
  color:var(--text);border-radius:10px;padding:11px 12px;font-size:15px;outline:none}
.search input:focus,.controls input[type=date]:focus,.sig-form input:focus,.sig-form select:focus{border-color:var(--cyan)}
.asset-list{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:20;display:none;
  background:var(--panel2);border:1px solid var(--border);border-radius:10px;overflow:hidden;
  max-height:320px;overflow-y:auto;box-shadow:0 12px 30px rgba(0,0,0,.5)}
.match{padding:10px 12px;cursor:pointer;font-family:ui-monospace,Menlo,monospace;font-size:14px}
.match:hover{background:rgba(139,92,246,.18)}
.match.empty{color:var(--muted);cursor:default}
.match .pay{float:right;color:#4ade80;font-weight:700}
.match .pay.closed{color:var(--muted)}
.match-count{padding:8px 12px;color:var(--muted);font-size:12px;border-top:1px solid var(--border);
  position:sticky;bottom:0;background:var(--panel2)}
.controls button,.btn{background:linear-gradient(135deg,var(--violet),var(--cyan));color:#fff;border:0;
  border-radius:10px;padding:11px 22px;font-size:15px;font-weight:700;cursor:pointer}
.controls button:hover,.btn:hover{filter:brightness(1.12)}
.btn.ghost{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:8px 14px;font-size:13px}
.btn.ghost:hover{border-color:var(--cyan)}
.btn.ghost.active{background:linear-gradient(135deg,var(--violet),var(--cyan));border-color:transparent;color:#fff}
.auto-toggle{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer;white-space:nowrap}
.auto-toggle input{accent-color:var(--violet);width:16px;height:16px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 2px 12px;align-items:center;color:var(--muted);font-size:13px}
.chip{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:999px;
  padding:6px 12px;font-size:13px;cursor:pointer;font-family:ui-monospace,Menlo,monospace}
.chip:hover{border-color:var(--cyan);color:#67e8f9}
.chip .pay{color:#4ade80;font-weight:700}
.err{color:#fb7185;margin:0 2px 12px;font-size:14px}
.err:empty{display:none}
.asset-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.asset-name{font-size:30px;font-weight:800;font-family:ui-monospace,Menlo,monospace;letter-spacing:.5px;line-height:1.1}
.asset-date{color:var(--muted);font-size:13px;margin-top:4px}
.payout-line{color:#4ade80;font-size:13px;font-weight:700;margin-top:4px;font-family:ui-monospace,Menlo,monospace}
.payout-line:empty{display:none}
.asset-head-right{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.head-price{text-align:right}
.head-label{color:var(--muted);font-size:10px;letter-spacing:2px}
.last-price{font-family:ui-monospace,Menlo,monospace;font-size:30px;font-weight:800;line-height:1.15;font-variant-numeric:tabular-nums}
.last-price.up{color:#4ade80}
.last-price.down{color:#fb7185}
.chg-line{font-family:ui-monospace,Menlo,monospace;font-size:16px;font-weight:700}
.chg-line.up{color:#4ade80}
.chg-line.down{color:#fb7185}
.badge{padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
.badge.live{background:rgba(34,197,94,.18);color:#4ade80;animation:pulse 1.6s infinite}
.badge.delayed{background:rgba(245,158,11,.18);color:#fbbf24}
.badge.stale{background:rgba(244,63,94,.18);color:#fb7185}
.badge.hist{background:rgba(255,255,255,.08);color:var(--muted)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.stat .label{color:var(--muted);font-size:11px;letter-spacing:1px;text-transform:uppercase}
.stat .value{font-size:20px;font-weight:700;margin-top:4px;font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.status-line{display:flex;gap:16px;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:13px}
.status-line .ok{color:#4ade80}
.status-line .warn{color:#fbbf24}
.auto-progress{width:120px;height:6px;border-radius:99px;background:#33333c;overflow:hidden;display:inline-block;vertical-align:middle}
.auto-progress span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--violet),var(--cyan))}
.bot-status{font-size:13px;line-height:1.9;color:var(--muted)}
.bot-status b{color:var(--text)}
.bot-status .ok{color:#4ade80}
.bot-status .warn{color:#fbbf24}
.bot-status .bad{color:#fb7185}
.chart-card{padding:10px 12px}
.chart-toolbar{display:flex;gap:8px;align-items:center;margin:2px 4px 10px;flex-wrap:wrap}
.chart-toolbar .title{font-weight:700;font-size:13px;letter-spacing:1px;color:var(--muted);margin-right:4px}
.tf-btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:999px;
  padding:5px 13px;font-size:13px;cursor:pointer;font-weight:700}
.tf-btn:hover{border-color:var(--cyan)}
.tf-btn.active{background:linear-gradient(135deg,var(--violet),var(--cyan));border-color:transparent;color:#fff}
.live-countdown{margin-left:auto;font-family:ui-monospace,Menlo,monospace;font-size:13px;font-weight:700;color:#67e8f9;white-space:nowrap}
.live-countdown.stale{color:#fb7185}
.live-countdown:empty{display:none}
.zoom-btn{font-size:15px;padding:5px 12px}
canvas{width:100%;height:240px;display:block;cursor:grab;touch-action:none}
.marker-note{color:var(--muted);font-size:12px;margin:6px 4px 0}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:8px 4px 0}
.legend span b{font-family:ui-monospace,Menlo,monospace}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:middle}
.table-card{padding:6px 0 10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:560px}
td,th{padding:7px 12px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-size:13px;border-bottom:1px solid #2e2e36;font-variant-numeric:tabular-nums}
td:first-child,th:first-child{text-align:left}
thead th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-size:11px;letter-spacing:1px;text-transform:uppercase}
tbody tr:nth-child(even){background:rgba(255,255,255,.02)}
tbody tr:hover{background:rgba(139,92,246,.08)}
tr.gap-row td{background:rgba(244,63,94,.16);color:#fb7185;text-align:center !important;font-size:12px;font-weight:700;letter-spacing:.5px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700}
.pill.call{background:rgba(34,197,94,.15);color:#4ade80}
.pill.put{background:rgba(244,63,94,.15);color:#fb7185}
.pill.doji{background:rgba(245,158,11,.15);color:#fbbf24}
.empty{color:var(--muted);text-align:center !important;padding:18px !important}
.row-count{color:var(--muted);font-size:12px;margin:10px 14px 0}
.dl-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:0 14px 10px}
.panel-title{font-weight:700;font-size:13px;letter-spacing:1px;color:var(--muted);margin:0 0 10px}
.sig-form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.sig-form input,.sig-form select{width:auto;min-width:110px;padding:9px 10px;font-size:14px}
.sig-form label{color:var(--muted);font-size:12px}
.sig-list{font-family:ui-monospace,Menlo,monospace;font-size:14px;line-height:2}
.sig-list .win{color:#4ade80}
.cmp-picker{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.cmp-picker select{background:#17171b;border:1px solid var(--border);color:var(--text);border-radius:10px;padding:9px 10px;font-size:14px;outline:none}
.cmp-tag{background:var(--panel2);border:1px solid var(--border);border-radius:999px;padding:5px 6px 5px 12px;font-size:13px;font-family:ui-monospace,Menlo,monospace}
.cmp-tag button{background:none;border:0;color:#fb7185;cursor:pointer;font-size:14px;padding:0 4px}
#cmp{height:220px;cursor:default}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:18px;line-height:1.7}
@media (max-width:640px){
  .brand h1{font-size:17px}
  .asset-name{font-size:22px}
  .last-price{font-size:22px}
  .controls button{width:100%}
}
"""

JS = """
const init = JSON.parse(document.getElementById('init').textContent);
const ASSETS = init.assets || [];
let cur = {asset: init.asset, date: init.date, rows: init.rows || []};
let autoOn = true;
let countdown = 30;
const $ = id => document.getElementById(id);

function fmtPrice(v){
  v = Number(v);
  if (v >= 1000) return v.toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
  if (v >= 100) return v.toFixed(3);
  return v.toFixed(5);
}
function esc(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toMin(t){ const p = t.split(':'); return (+p[0]) * 60 + (+p[1]); }
function toHHMM(m){ return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'); }
function dirOf(r){ return r.close > r.open ? 'CALL' : (r.close < r.open ? 'PUT' : 'DOJI'); }
function pillOf(r){ const d = dirOf(r); return '<span class="pill ' + d.toLowerCase() + '">' + d + '</span>'; }

function computeGaps(rows){
  if (rows.length < 2) return {count: 0, ranges: []};
  const mins = rows.map(r => toMin(r.time));
  const have = new Set(mins);
  const missing = [];
  for (let m = mins[0]; m <= mins[mins.length - 1]; m++){ if (!have.has(m)) missing.push(m); }
  const ranges = [];
  for (let i = 0; i < missing.length;){
    let j = i;
    while (j + 1 < missing.length && missing[j + 1] === missing[j] + 1) j++;
    ranges.push([missing[i], missing[j]]);
    i = j + 1;
  }
  return {count: missing.length, ranges: ranges};
}
function gapsText(g){
  if (!g.count) return '<span class="ok">None</span>';
  const parts = g.ranges.slice(0, 4).map(r => r[0] === r[1] ? toHHMM(r[0]) : toHHMM(r[0]) + '–' + toHHMM(r[1]));
  let s = g.count + ' min — ' + parts.join(', ');
  if (g.ranges.length > 4) s += ' (+' + (g.ranges.length - 4) + ' more)';
  return '<span class="warn">' + s + '</span>';
}
function dhakaNow(){
  const d = new Date();
  const time = d.toLocaleTimeString('en-GB', {timeZone: 'Asia/Dhaka', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const date = d.toLocaleDateString('en-CA', {timeZone: 'Asia/Dhaka'});
  return {time: time, date: date, hhmm: time.slice(0, 5), second: +time.slice(6, 8),
          min: (+time.slice(0, 2)) * 60 + (+time.slice(3, 5))};
}
function statusOf(rows){
  const now = dhakaNow();
  if (!rows.length) return {cls: 'stale', label: 'NO DATA'};
  if (cur.date !== now.date) return {cls: 'hist', label: 'HISTORY'};
  const last = toMin(rows[rows.length - 1].time);
  const diff = now.min - last;
  if (diff <= 2) return {cls: 'live', label: '● LIVE'};
  if (diff <= 10) return {cls: 'delayed', label: 'DELAYED ' + diff + 'm'};
  return {cls: 'stale', label: 'STALE ' + diff + 'm'};
}

// Chart timeframe in minutes (1 / 5 / 15 / 60). It survives reloads:
// the URL carries ?tf= and the last choice is kept in localStorage,
// so a refresh (or a full page reload) never silently drops back to 15m.
function initialTf(){
  try {
    const u = new URLSearchParams(location.search).get('tf');
    if (u && [1, 5, 15, 60].indexOf(+u) !== -1) return +u;
    const s = localStorage.getItem('viewer_tf');
    if (s && [1, 5, 15, 60].indexOf(+s) !== -1) return +s;
  } catch (e){}
  return 15;
}
let tf = initialTf();
let loadToken = 0;      // guards the loadAsset retry (see below)
let payouts = {};       // asset -> {payout, open, updated_at}
let liveTick = null;    // last /api/live_tick result {price, ts, stale}
let liveCandle = null;  // forming candle {minute HH:MM, open, high, low, close}

function aggregate(rows, tfMin){
  // Group 1-min rows into tfMin-minute candles, aligned to the day:
  // open = first open, high = max high, low = min low, close = last close.
  if (tfMin <= 1) return rows.map(r => Object.assign({}, r, {bucket: toMin(r.time), min: toMin(r.time)}));
  const out = [];
  let curB = null;
  rows.forEach(r => {
    const b = Math.floor(toMin(r.time) / tfMin);
    if (!curB || curB.bucket !== b){
      curB = {bucket: b, min: b * tfMin, time: r.time, open: r.open, high: r.high, low: r.low, close: r.close, live: !!r.live};
      out.push(curB);
    } else {
      if (r.high > curB.high) curB.high = r.high;
      if (r.low < curB.low) curB.low = r.low;
      curB.close = r.close;
      if (r.live) curB.live = true;
    }
  });
  return out;
}

function liveRow(){
  return {time: liveCandle.minute, open: liveCandle.open, high: liveCandle.high,
          low: liveCandle.low, close: liveCandle.close, live: true};
}
// Closed candles + the forming live candle (only for today).
function displayRows(){
  const rows = cur.rows;
  if (!liveCandle || cur.date !== dhakaNow().date) return rows;
  const lm = liveCandle.minute;
  if (!rows.length) return [liveRow()];
  const last = rows[rows.length - 1];
  if (lm > last.time) return rows.concat([liveRow()]);
  if (lm === last.time){ const cp = rows.slice(); cp[cp.length - 1] = liveRow(); return cp; }
  return rows;  // tick minute is behind the closed data — ignore it
}
function aggRows(){ return aggregate(displayRows(), tf); }

function isTickStale(){
  return !liveTick || liveTick.ts == null || (Date.now() / 1000 - liveTick.ts > 10);
}
function headerPrice(){
  if (liveTick && liveTick.price != null && !isTickStale()) return liveTick.price;
  const rows = cur.rows;
  return rows.length ? rows[rows.length - 1].close : null;
}

function drawChart(){
  const cv = $('ch'), ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const all = aggRows();
  const total = all.length;
  let a = 0, b = total;
  if (view){ a = Math.max(0, Math.floor(view[0])); b = Math.min(total, Math.ceil(view[1])); }
  const candles = all.slice(a, b);
  if (candles.length < 2){
    ctx.fillStyle = '#9a9ca8'; ctx.font = '14px system-ui';
    ctx.fillText('No data to chart', 16, H / 2);
    return;
  }
  let mn = Infinity, mx = -Infinity;
  candles.forEach(c => { if (c.low < mn) mn = c.low; if (c.high > mx) mx = c.high; });
  const rg = (mx - mn) || 1;
  const padL = 8, padR = 62, padT = 16, padB = 22;
  const slotW = (W - padL - padR) / candles.length;
  const bodyW = Math.max(1, Math.floor(slotW * 0.7));
  const X = i => padL + i * slotW + slotW / 2;
  const Y = v => padT + (1 - (v - mn) / rg) * (H - padT - padB);
  // grid + price labels on the right
  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  ctx.fillStyle = '#9a9ca8'; ctx.font = '11px system-ui'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++){
    const v = mn + rg * i / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR + 6, y); ctx.stroke();
    ctx.fillText(fmtPrice(v), W - padR + 10, y + 4);
  }
  // red gap bands: aggregated buckets that contain missing minutes
  const gaps = computeGaps(cur.rows);
  if (gaps.count){
    const bmap = {};
    all.forEach((c, i) => { bmap[c.bucket] = i; });
    const missIdx = new Set();
    gaps.ranges.forEach(r => {
      for (let m = r[0]; m <= r[1]; m++){
        const idx = bmap[Math.floor(m / tf)];
        if (idx !== undefined) missIdx.add(idx);
      }
    });
    ctx.fillStyle = 'rgba(244,63,94,0.13)';
    let run = null;
    const flush = end => {
      if (run === null) return;
      const x0 = padL + (run - a) * slotW, x1 = padL + (end - a) * slotW;
      ctx.fillRect(x0, padT, Math.max(2, x1 - x0), H - padT - padB);
      run = null;
    };
    for (let i = a; i < b; i++){
      if (missIdx.has(i)){ if (run === null) run = i; }
      else flush(i);
    }
    flush(b);
  }
  // candles
  candles.forEach((c, i) => {
    const col = c.close > c.open ? '#22c55e' : (c.close < c.open ? '#f43f5e' : '#f59e0b');
    const x = X(i);
    ctx.strokeStyle = col; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, Y(c.high)); ctx.lineTo(x, Y(c.low)); ctx.stroke();
    const yO = Y(c.open), yC = Y(c.close);
    ctx.fillStyle = col;
    if (Math.abs(yO - yC) < 1){
      ctx.fillRect(x - bodyW / 2, yO - 0.5, bodyW, 1);
    } else {
      ctx.fillRect(x - bodyW / 2, Math.min(yO, yC), bodyW, Math.abs(yO - yC));
    }
    // forming live candle: dashed outline so it reads as "not closed yet"
    if (c.live){
      ctx.strokeStyle = '#e5e7eb'; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.strokeRect(x - bodyW / 2 - 1.5, Math.min(Y(c.high), Y(c.low)) - 2, bodyW + 3,
        Math.abs(Y(c.high) - Y(c.low)) + 4);
      ctx.setLineDash([]);
      ctx.fillStyle = '#e5e7eb'; ctx.font = 'bold 10px system-ui';
      ctx.fillText('LIVE', Math.max(4, x - 12), Math.max(12, Y(c.high) - 6));
    }
  });
  const last = candles[candles.length - 1];
  const lastCol = last.close > last.open ? '#22c55e' : (last.close < last.open ? '#f43f5e' : '#f59e0b');
  ctx.fillStyle = '#9a9ca8'; ctx.font = '12px system-ui';
  ctx.fillText(candles[0].time, 10, H - 6);
  ctx.fillText(last.time, W - padR - 36, H - 6);
  ctx.fillStyle = lastCol; ctx.font = 'bold 13px system-ui';
  ctx.fillText(fmtPrice(last.close), Math.max(10, X(candles.length - 1) - 70), Math.max(16, Y(last.close) - 10));
}

function syncTfButtons(){
  document.querySelectorAll('.tf-btn[data-tf]').forEach(x =>
    x.classList.toggle('active', +x.dataset.tf === tf));
}
document.querySelectorAll('.tf-btn[data-tf]').forEach(b => {
  b.addEventListener('click', () => {
    tf = parseInt(b.dataset.tf, 10) || 15;
    try { localStorage.setItem('viewer_tf', String(tf)); } catch (e){}
    history.replaceState(null, '', '/?asset=' + encodeURIComponent(cur.asset) +
      '&date=' + encodeURIComponent(cur.date) + '&tf=' + tf);
    syncTfButtons();
    view = null;  // new timeframe = full day again
    drawChart();
  });
});

// Zoom + pan (the owner, 2026-11-27): wheel zooms around the cursor,
// drag pans, + / − buttons zoom for touch screens, Full day resets.
// view = [a, b] float indices into the aggregated candles, null = full day.
let view = null;
const MIN_WIN = 10;

function clampView(a, b, total){
  const minLen = Math.min(MIN_WIN, total);
  let len = b - a;
  if (len < minLen){ const c = (a + b) / 2; a = c - minLen / 2; b = c + minLen / 2; len = minLen; }
  if (len > total){ a = 0; b = total; }
  if (a < 0){ b -= a; a = 0; }
  if (b > total){ a -= (b - total); b = total; }
  if (a < 0) a = 0;
  return [a, b];
}
function zoomView(a, b, total, center, factor){
  const len = (b - a) * factor;
  const na = center - (center - a) * factor;
  return clampView(na, na + len, total);
}
function panView(a, b, total, delta){
  return clampView(a + delta, b + delta, total);
}
function setView(nv, total){
  view = (nv[1] - nv[0] >= total - 0.5) ? null : nv;
  drawChart();
}
// After new candles arrive, where should the zoomed view go? If it
// was pinned to the live (right) edge, slide it right by the new
// candles so the latest stays visible; if it was looking at older
// candles, leave it exactly where it was; a full view stays full.
function followView(view, oldTotal, newTotal){
  if (!view) return null;
  if (view[1] - view[0] >= oldTotal - 0.5) return null;
  if (view[1] < oldTotal - 0.5) return view;
  const len = view[1] - view[0];
  return [newTotal - len, newTotal];
}
function zoomCenter(factor){
  const total = aggRows().length;
  if (total < 2) return;
  let a = 0, b = total;
  if (view){ a = view[0]; b = view[1]; }
  setView(zoomView(a, b, total, (a + b) / 2, factor), total);
}
$('zoomIn').addEventListener('click', () => zoomCenter(0.7));
$('zoomOut').addEventListener('click', () => zoomCenter(1.4));
$('zoomReset').addEventListener('click', () => { view = null; drawChart(); });

const cvEl = $('ch');
function canvasX(clientX){
  const rect = cvEl.getBoundingClientRect();
  return (clientX - rect.left) * (cvEl.width / rect.width);
}
cvEl.addEventListener('wheel', e => {
  e.preventDefault();
  const total = aggRows().length;
  if (total < 2) return;
  let a = 0, b = total;
  if (view){ a = view[0]; b = view[1]; }
  const slotW = (cvEl.width - 16) / (b - a);
  let idx = a + Math.floor((canvasX(e.clientX) - 8) / slotW);
  idx = Math.max(a, Math.min(b - 1, idx));
  setView(zoomView(a, b, total, idx, e.deltaY < 0 ? 0.8 : 1.25), total);
}, {passive: false});

let drag = null;
function dragStart(clientX){
  drag = {x: clientX, view: view ? view.slice() : null};
  cvEl.style.cursor = 'grabbing';
}
function dragMove(clientX){
  if (!drag) return;
  const total = aggRows().length;
  if (total < 2) return;
  let a = 0, b = total;
  if (drag.view){ a = drag.view[0]; b = drag.view[1]; }
  const slotW = (cvEl.width - 16) / (b - a);
  const delta = -(canvasX(clientX) - canvasX(drag.x)) / slotW;
  setView(panView(a, b, total, delta), total);
}
function dragEnd(){ drag = null; cvEl.style.cursor = 'grab'; }
cvEl.addEventListener('mousedown', e => dragStart(e.clientX));
window.addEventListener('mousemove', e => dragMove(e.clientX));
window.addEventListener('mouseup', dragEnd);
cvEl.addEventListener('touchstart', e => { if (e.touches.length === 1) dragStart(e.touches[0].clientX); }, {passive: true});
cvEl.addEventListener('touchmove', e => { if (e.touches.length === 1){ e.preventDefault(); dragMove(e.touches[0].clientX); } }, {passive: false});
cvEl.addEventListener('touchend', dragEnd);

function updateBadge(){
  const b = $('liveBadge');
  if (liveTick && liveTick.price != null){
    if (!isTickStale()){ b.className = 'badge live'; b.textContent = '● LIVE'; return; }
    b.className = 'badge stale'; b.textContent = 'STALE — no tick 10s+'; return;
  }
  const st = statusOf(cur.rows);
  b.className = 'badge ' + st.cls; b.textContent = st.label;
}

function updateHeaderPrice(){
  const rows = cur.rows;
  const lp = $('lastPrice'), chg = $('chgLine');
  const price = headerPrice();
  const firstO = rows.length ? rows[0].open : null;
  if (price !== null && price !== undefined){
    lp.textContent = fmtPrice(price);
    if (firstO !== null && firstO !== 0){
      const pct = (price - firstO) / firstO * 100;
      const up = pct >= 0;
      lp.className = 'last-price ' + (up ? 'up' : 'down');
      chg.textContent = (up ? '▲ +' : '▼ ') + pct.toFixed(2) + '% vs day open';
      chg.className = 'chg-line ' + (up ? 'up' : 'down');
    } else { lp.className = 'last-price'; chg.textContent = ''; }
  } else { lp.textContent = '—'; lp.className = 'last-price'; chg.textContent = ''; }
}

function renderAll(){
  syncTfButtons();
  const rows = cur.rows;
  $('statCount').textContent = rows.length;
  $('statFirst').textContent = rows.length ? rows[0].time : '—';
  $('statLast').textContent = rows.length ? rows[rows.length - 1].time : '—';
  if (rows.length){
    let hi = -Infinity, lo = Infinity;
    rows.forEach(r => { if (r.high > hi) hi = r.high; if (r.low < lo) lo = r.low; });
    $('statHigh').textContent = fmtPrice(hi);
    $('statLow').textContent = fmtPrice(lo);
  } else { $('statHigh').textContent = '—'; $('statLow').textContent = '—'; }
  updateBadge();
  $('gapsLine').innerHTML = 'Gaps: ' + gapsText(computeGaps(rows));
  $('assetTitle').textContent = cur.asset;
  $('dateTitle').textContent = cur.date + ' (Dhaka time)';
  const p = payouts[cur.asset];
  $('payoutLine').textContent = (p && p.payout != null)
    ? ('Payout ' + p.payout + '%' + (p.open === false ? ' (closed)' : '')) : '';
  updateHeaderPrice();
  let h = '';
  let prevMin = null;
  rows.forEach(r => {
    const m = toMin(r.time);
    if (prevMin !== null && m - prevMin > 1){
      const n = m - prevMin - 1;
      h += '<tr class="gap-row"><td colspan="6">GAP — ' + n + ' min missing (' +
        toHHMM(prevMin + 1) + (n > 1 ? ' – ' + toHHMM(m - 1) : '') + ')</td></tr>';
    }
    prevMin = m;
    h += '<tr><td>' + r.time + '</td><td>' + fmtPrice(r.open) + '</td><td>' + fmtPrice(r.high) +
      '</td><td>' + fmtPrice(r.low) + '</td><td>' + fmtPrice(r.close) + '</td><td>' + pillOf(r) + '</td></tr>';
  });
  $('tbody').innerHTML = h || '<tr><td colspan="6" class="empty">No candles for this asset on this date.</td></tr>';
  $('rowCount').textContent = rows.length + ' candles';
  drawChart();
}

async function loadAsset(asset, date, keepView, _retried){
  asset = (asset || '').trim();
  if (!asset) return;
  const myToken = ++loadToken;
  const url = '/api/candles?asset=' + encodeURIComponent(asset) + '&date=' + encodeURIComponent(date || cur.date);
  let j = null;
  try {
    const r = await fetch(url);
    j = await r.json();
    if (!r.ok || !j || j.error || !Array.isArray(j.candles)){
      throw new Error((j && j.error) ? j.error : 'bad response');
    }
  } catch (e){
    // A failed load (502 / DB blip) must not blank the page: keep the
    // old chart and retry once after 2s before giving up. The token
    // guard stops a stale retry from overwriting a newer asset.
    if (!_retried){
      $('errLine').textContent = 'Could not load candles — retrying…';
      setTimeout(() => { if (myToken === loadToken) loadAsset(asset, date, keepView, true); }, 2000);
    } else {
      $('errLine').textContent = 'Could not load candles — check connection and retry.';
    }
    return;
  }
  {
    // Bug fix (the owner, 2026-11-27): the 30s auto-refresh calls
    // loadAsset for the SAME asset/date — it used to reset view to
    // null every time, so the chart zoomed out whenever a new candle
    // arrived. Now a refresh (keepView) keeps the zoom; only a real
    // asset/date change resets to the full day.
    const oldTotal = aggRows().length;
    const oldView = view;
    cur.asset = j.asset; cur.date = j.date; cur.rows = j.candles || [];
    if (keepView){
      view = followView(oldView, oldTotal, aggRows().length);
    } else {
      view = null; liveCandle = null; liveTick = null;
    }
    document.title = 'Candles — ' + cur.asset + ' ' + cur.date;
    history.replaceState(null, '', '/?asset=' + encodeURIComponent(cur.asset) + '&date=' + encodeURIComponent(cur.date) + '&tf=' + tf);
    $('dateInput').value = cur.date;
    $('sigAsset').value = cur.asset; $('sigDate').value = cur.date;
    $('errLine').textContent = cur.rows.length ? '' : 'No candles for this asset on this date — try another date.';
    renderAll();
    setUpdated();
    countdown = 30;
    pollLiveTick();
  }
}

function setUpdated(){ $('updatedAt').textContent = 'Updated ' + dhakaNow().time + ' (Dhaka)'; }
function refreshIfToday(){
  if (!autoOn) return;
  if (cur.date !== dhakaNow().date) return;
  loadAsset(cur.asset, cur.date, true);  // refresh keeps the zoom
}

// --- payouts: shown in search dropdown, quick chips and the header ---
async function loadPayouts(){
  try {
    const r = await fetch('/api/payouts');
    const j = await r.json();
    (j.payouts || []).forEach(p => { payouts[p.asset] = p; });
    document.querySelectorAll('.chip').forEach(c => {
      const p = payouts[c.dataset.a];
      if (p && p.payout != null && !c.querySelector('.pay')){
        c.innerHTML = esc(c.dataset.a) + ' <span class="pay">' + p.payout + '%</span>';
      }
    });
    renderAll();
  } catch (e){ /* payouts are a bonus — the viewer works without them */ }
}
function payLabel(a){
  const p = payouts[a];
  if (!p || p.payout == null) return '';
  return '<span class="pay' + (p.open === false ? ' closed' : '') + '">' + p.payout + '%</span>';
}

// --- bot status bar ---
async function loadBotStatus(){
  try {
    const r = await fetch('/api/bot_status');
    const j = await r.json();
    const stopped = Object.entries(j.stopped || {}).map(e => esc(e[0]) + ' (' + esc(e[1]) + ')').join(', ') || 'none';
    const resumed = (j.resumed || []).map(e => esc(e[0]) + ' (' + esc(e[1]) + ')').join(', ') || 'none';
    $('botStatus').innerHTML =
      '<b>Last collection:</b> ' + esc(j.last_collection || '—') +
      ' &nbsp; <b>Next:</b> ' + esc(j.next_collection || '—') + '<br>' +
      '<b>Live stream:</b> <span class="' + (j.live_stream_status === 'connected' ? 'ok' : 'warn') + '">' +
      esc(j.live_stream_status || 'not running') + '</span>' +
      ' — last candle ' + esc(j.live_stream_last_seen || '—') + '<br>' +
      '<b>Stopped:</b> <span class="bad">' + stopped + '</span>' +
      ' &nbsp; <b>Resumed:</b> <span class="ok">' + resumed + '</span><br>' +
      '<b>Quotex:</b> ' + esc(j.quotex_session || '—') +
      ' &nbsp; <b>Last login:</b> ' + esc(j.quotex_last_login || '—') +
      ' &nbsp; <b>Bot:</b> free ' + (j.bot_on_free ? 'ON' : 'OFF') +
      ' / premium ' + (j.bot_on_premium ? 'ON' : 'OFF') +
      (j.total_candles != null ? ' &nbsp; <b>DB:</b> ' + j.total_candles + ' candles' : '');
  } catch (e){
    $('botStatus').textContent = 'Bot status unavailable — the bot may not have written its state yet.';
  }
}

// --- live tick (Fix 11): 1s poll, forming candle, countdown ---
async function pollLiveTick(){
  if (!cur.asset) return;
  try {
    const r = await fetch('/api/live_tick?asset=' + encodeURIComponent(cur.asset));
    handleTick(await r.json());
  } catch (e){ /* keep the last tick state; the badge falls back to candle status */ }
}
function handleTick(j){
  if (!j || j.price == null || j.ts == null){
    liveTick = null; liveCandle = null; updateBadge(); return;
  }
  liveTick = {price: j.price, ts: j.ts, stale: !!j.stale};
  const now = dhakaNow();
  if (cur.date !== now.date){ liveCandle = null; updateBadge(); updateHeaderPrice(); return; }
  if (j.open != null && j.high != null && j.low != null && j.close != null){
    // Rich forming-candle form: the bot already tracked OHLC.
    let minute = (j.minute || '').slice(11, 16) || now.hhmm;
    if (j.minute && j.minute.slice(0, 10) !== cur.date) minute = now.hhmm;
    liveCandle = {minute: minute, open: j.open, high: j.high, low: j.low, close: j.close};
  } else {
    // Simple price-only form: track high/low client-side;
    // open = the last closed candle's close.
    const minute = now.hhmm;
    if (!liveCandle || liveCandle.minute !== minute){
      const last = cur.rows[cur.rows.length - 1];
      const open = last ? last.close : j.price;
      liveCandle = {minute: minute, open: open, high: j.price, low: j.price, close: j.price};
    } else {
      if (j.price > liveCandle.high) liveCandle.high = j.price;
      if (j.price < liveCandle.low) liveCandle.low = j.price;
      liveCandle.close = j.price;
    }
  }
  updateHeaderPrice();
  updateBadge();
  drawChart();
}

// --- compare 2–3 assets (normalized % change, indexed to 100) ---
const CMP_COLORS = ['#22c55e', '#06b6d4', '#a78bfa'];
let compareAssets = [];
let compareRows = {};
function renderCmpTags(){
  $('cmpTags').innerHTML = compareAssets.map((a, i) =>
    '<span class="cmp-tag"><span class="sw" style="background:' + CMP_COLORS[i] + '"></span>' + esc(a) +
    ' <button type="button" data-a="' + esc(a) + '" title="Remove">×</button></span>').join('');
  $('cmpTags').querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => {
      compareAssets = compareAssets.filter(x => x !== b.dataset.a);
      delete compareRows[b.dataset.a];
      renderCmpTags(); drawCompare();
    });
  });
}
async function addCompare(asset){
  asset = (asset || '').trim();
  if (!asset || compareAssets.includes(asset)) return;
  if (compareAssets.length >= 3){ $('cmpHint').textContent = 'Compare holds max 3 assets — remove one first.'; return; }
  $('cmpHint').textContent = '';
  try {
    const r = await fetch('/api/candles?asset=' + encodeURIComponent(asset) + '&date=' + encodeURIComponent(cur.date));
    const j = await r.json();
    compareAssets.push(asset);
    compareRows[asset] = j.candles || [];
    renderCmpTags(); drawCompare();
  } catch (e){ $('cmpHint').textContent = 'Could not load ' + asset; }
}
function drawCompare(){
  const cv = $('cmp');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const legend = $('cmpLegend');
  if (compareAssets.length < 2){
    ctx.fillStyle = '#9a9ca8'; ctx.font = '14px system-ui';
    ctx.fillText('Pick at least 2 assets above to compare them.', 16, H / 2);
    legend.innerHTML = '';
    return;
  }
  const series = compareAssets.map(a => {
    const rows = compareRows[a] || [];
    if (!rows.length) return null;
    const base = rows[0].close;
    return {asset: a, pts: rows.map(r => ({m: toMin(r.time), v: r.close / base * 100}))};
  }).filter(Boolean);
  if (series.length < 2){
    ctx.fillStyle = '#9a9ca8'; ctx.font = '14px system-ui';
    ctx.fillText('Not enough data to compare yet.', 16, H / 2);
    legend.innerHTML = '';
    return;
  }
  let mn = Infinity, mx = -Infinity;
  series.forEach(s => s.pts.forEach(p => { if (p.v < mn) mn = p.v; if (p.v > mx) mx = p.v; }));
  const pad = (mx - mn) * 0.1 || 1; mn -= pad; mx += pad;
  const X = m => 10 + m / 1439 * (W - 20);
  const Y = v => 12 + (1 - (v - mn) / (mx - mn)) * (H - 32);
  ctx.strokeStyle = 'rgba(255,255,255,0.07)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(10, Y(100)); ctx.lineTo(W - 10, Y(100)); ctx.stroke();
  ctx.fillStyle = '#9a9ca8'; ctx.font = '11px system-ui';
  ctx.fillText('100', 12, Y(100) - 4);
  series.forEach((s, i) => {
    ctx.strokeStyle = CMP_COLORS[i]; ctx.lineWidth = 2; ctx.beginPath();
    s.pts.forEach((p, k) => { if (k === 0) ctx.moveTo(X(p.m), Y(p.v)); else ctx.lineTo(X(p.m), Y(p.v)); });
    ctx.stroke();
  });
  legend.innerHTML = series.map((s, i) => {
    const chg = s.pts[s.pts.length - 1].v - 100;
    return '<span><span class="sw" style="background:' + CMP_COLORS[i] + '"></span><b>' + esc(s.asset) +
      '</b> ' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</span>';
  }).join('');
}
$('cmpAdd').addEventListener('click', () => addCompare($('cmpSelect').value));
$('cmpSelect').addEventListener('change', () => addCompare($('cmpSelect').value));

// --- downloads: JSON + CSV (from loaded candles), PNG (chart canvas) ---
function downloadBlob(name, blob){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}
function baseName(){ return cur.asset + '_' + cur.date; }
$('dlJson').addEventListener('click', () => {
  const data = {asset: cur.asset, date: cur.date, count: cur.rows.length, candles: cur.rows};
  downloadBlob(baseName() + '.json', new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
});
$('dlCsv').addEventListener('click', () => {
  const lines = ['time,open,high,low,close'];
  cur.rows.forEach(r => lines.push([r.time, r.open, r.high, r.low, r.close].join(',')));
  downloadBlob(baseName() + '.csv', new Blob([lines.join('\\n')], {type: 'text/csv'}));
});
$('dlPng').addEventListener('click', () => {
  $('ch').toBlob(b => { if (b) downloadBlob(baseName() + '_chart.png', b); }, 'image/png');
});

// --- signal generator (viewer port of the bot's futures signals) ---
$('sigForm').addEventListener('submit', async e => {
  e.preventDefault();
  const asset = $('sigAsset').value.trim() || cur.asset;
  const date = $('sigDate').value || cur.date;
  const days = Math.max(1, Math.min(10, parseInt($('sigDays').value, 10) || 5));
  const pct = Math.max(0, Math.min(100, parseInt($('sigPct').value, 10) || 70));
  $('sigResults').innerHTML = 'Loading…';
  try {
    const r = await fetch('/api/signals?asset=' + encodeURIComponent(asset) +
      '&date=' + encodeURIComponent(date) + '&days=' + days + '&pct=' + pct);
    const j = await r.json();
    if (!j.signals || !j.signals.length){
      $('sigResults').innerHTML = 'No signals — try a lower win% or more days. (Used ' + j.days +
        ' day(s): ' + (j.dates || []).join(', ') + '.)';
      return;
    }
    $('sigResults').innerHTML = '<b>' + j.count + ' signals</b> — from ' + j.days + ' day(s) (' +
      (j.dates || []).join(', ') + '), win% ≥ ' + j.threshold_pct + '%:<br>' +
      j.signals.map(s => esc(s.line) + ' — <span class="win">' + s.win_pct + '%</span> (' +
        s.calls + 'C/' + s.puts + 'P)</span>').join('<br>');
  } catch (err){
    $('sigResults').innerHTML = 'Could not generate signals — check connection and retry.';
  }
});

const search = $('assetSearch'), list = $('assetList');
function showMatches(q){
  q = (q || '').trim().toUpperCase();
  const m = ASSETS.filter(a => !q || a.toUpperCase().includes(q));
  if (!m.length){ list.innerHTML = '<div class="match empty">No asset matches.</div>'; list.style.display = 'block'; return; }
  list.innerHTML = m.map(a => '<div class="match" data-a="' + a + '">' + a + payLabel(a) + '</div>').join('') +
    '<div class="match-count">' + m.length + ' assets — scroll or type to filter</div>';
  list.style.display = 'block';
}
search.addEventListener('input', () => showMatches(search.value));
search.addEventListener('focus', () => showMatches(search.value));
document.addEventListener('click', e => { if (!e.target.closest('.search')) list.style.display = 'none'; });
list.addEventListener('click', e => {
  const t = e.target.closest('.match');
  if (!t || !t.dataset.a) return;
  search.value = t.dataset.a; list.style.display = 'none';
  loadAsset(t.dataset.a, $('dateInput').value);
});
search.addEventListener('keydown', e => {
  if (e.key === 'Enter'){
    e.preventDefault();
    const first = list.querySelector('.match[data-a]');
    if (first){ search.value = first.dataset.a; list.style.display = 'none'; loadAsset(first.dataset.a, $('dateInput').value); }
  }
});
document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('click', () => { search.value = c.dataset.a; loadAsset(c.dataset.a, $('dateInput').value); });
});
$('showForm').addEventListener('submit', e => { e.preventDefault(); loadAsset(search.value, $('dateInput').value); });
$('autoToggle').addEventListener('change', e => {
  autoOn = e.target.checked;
  $('autoPill').textContent = autoOn ? 'AUTO ON' : 'AUTO OFF';
  $('autoPill').className = 'auto-pill ' + (autoOn ? 'on' : 'off');
  countdown = 30;
});
setInterval(() => { $('clock').textContent = dhakaNow().time + ' Dhaka'; }, 1000);
setInterval(() => {
  if (!autoOn) return;
  countdown--;
  $('autoBar').style.width = ((30 - countdown) / 30 * 100) + '%';
  $('autoText').textContent = 'Next refresh in ' + countdown + 's';
  if (countdown <= 0){ countdown = 30; refreshIfToday(); }
}, 1000);
setInterval(() => {
  // Live-candle countdown, every second — always visible in the chart
  // header while viewing today (Quotex-style), never blank.
  const el = $('liveCountdown');
  const now = dhakaNow();
  if (cur.date !== now.date){ el.textContent = ''; el.className = 'live-countdown'; return; }
  if (liveTick && liveTick.price != null){
    if (isTickStale()){
      el.textContent = 'Live tick stale — last tick ' +
        Math.max(0, Math.round(Date.now() / 1000 - liveTick.ts)) + 's ago';
      el.className = 'live-countdown stale';
      updateBadge();
    } else {
      el.textContent = '● LIVE — candle closes in ' + (59 - now.second) + 's';
      el.className = 'live-countdown';
    }
  } else {
    el.textContent = 'Live candle — waiting for tick…';
    el.className = 'live-countdown';
  }
}, 1000);
renderAll();
setUpdated();
$('clock').textContent = dhakaNow().time + ' Dhaka';
$('sigAsset').value = cur.asset; $('sigDate').value = cur.date;
$('cmpSelect').innerHTML = ASSETS.map(a => '<option value="' + a + '">' + a + '</option>').join('');
loadPayouts();
loadBotStatus();
pollLiveTick();
setInterval(pollLiveTick, 1000);
setInterval(loadBotStatus, 60000);
"""


def page_html(asset, date_str, assets, rows, error="", tf=15):
    trs = "".join(
        f"<tr><td>{r['time']}</td><td>{fmt_price(r['open'])}</td>"
        f"<td>{fmt_price(r['high'])}</td><td>{fmt_price(r['low'])}</td>"
        f"<td>{fmt_price(r['close'])}</td><td>{dir_pill(r)}</td></tr>"
        for r in rows
    )
    popular = [a for a in ("BTCUSD_otc", "EURUSD_otc", "USDPKR_otc", "USDMXN_otc")
               if a in assets]
    chips = "".join(
        f'<button type="button" class="chip" data-a="{html.escape(a)}">'
        f"{html.escape(a)}</button>" for a in popular
    )
    hi = fmt_price(max((r["high"] for r in rows), default=0)) if rows else "—"
    lo = fmt_price(min((r["low"] for r in rows), default=0)) if rows else "—"
    init = json.dumps({"asset": asset, "date": date_str, "assets": assets, "rows": rows})
    err = html.escape(error) if error else ""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Candles — {html.escape(asset or '')} {html.escape(date_str)}</title>
<style>{CSS}</style>
</head><body><div class="topbar"></div><div class="wrap">
<header class="hero card">
  <div class="brand"><div class="logo">BF</div><div>
    <h1>BLUE FISH <span>PRIME</span></h1>
    <p>LITTLE MATRIX &bull; CANDLE VIEWER</p>
  </div></div>
  <div class="hero-right">
    <div id="clock" class="clock">--:--:-- Dhaka</div>
    <span id="autoPill" class="auto-pill on">AUTO ON</span>
  </div>
</header>
<form id="showForm" class="card controls" method="get" action="/">
  <div class="search">
    <input id="assetSearch" name="asset" type="text" value="{html.escape(asset or '')}"
      placeholder="Search asset... e.g. EURUSD" autocomplete="off" spellcheck="false">
    <div id="assetList" class="asset-list"></div>
  </div>
  <input id="dateInput" name="date" type="date" value="{html.escape(date_str)}">
  <button type="submit">Show</button>
  <label class="auto-toggle"><input id="autoToggle" type="checkbox" checked> Auto-refresh</label>
</form>
<div class="chips">Quick: {chips}</div>
<p id="errLine" class="err">{err}</p>
<div class="asset-head card">
  <div>
    <div id="assetTitle" class="asset-name">{html.escape(asset or '')}</div>
    <div id="dateTitle" class="asset-date">{html.escape(date_str)} (Dhaka time)</div>
    <div id="payoutLine" class="payout-line"></div>
  </div>
  <div class="asset-head-right">
    <div class="head-price">
      <div class="head-label">LIVE PRICE</div>
      <div id="lastPrice" class="last-price">—</div>
      <div id="chgLine" class="chg-line"></div>
    </div>
    <span id="liveBadge" class="badge hist">—</span>
  </div>
</div>
<div class="stats">
  <div class="stat"><div class="label">Candles</div><div class="value" id="statCount">{len(rows)}</div></div>
  <div class="stat"><div class="label">First</div><div class="value" id="statFirst">{rows[0]['time'] if rows else '—'}</div></div>
  <div class="stat"><div class="label">Last</div><div class="value" id="statLast">{rows[-1]['time'] if rows else '—'}</div></div>
  <div class="stat"><div class="label">Day High</div><div class="value" id="statHigh">{hi}</div></div>
  <div class="stat"><div class="label">Day Low</div><div class="value" id="statLow">{lo}</div></div>
</div>
<div class="status-line card">
  <span id="gapsLine">Gaps: —</span>
  <span id="updatedAt">Updated —</span>
  <span class="auto-progress"><span id="autoBar"></span></span>
  <span id="autoText">Next refresh in 30s</span>
</div>
<div class="card">
  <p class="panel-title">BOT STATUS</p>
  <div id="botStatus" class="bot-status">Loading bot status…</div>
</div>
<div class="card chart-card">
  <div class="chart-toolbar"><span class="title">CANDLE CHART</span>
    <button type="button" class="tf-btn{' active' if tf == 1 else ''}" data-tf="1">1m</button>
    <button type="button" class="tf-btn{' active' if tf == 5 else ''}" data-tf="5">5m</button>
    <button type="button" class="tf-btn{' active' if tf == 15 else ''}" data-tf="15">15m</button>
    <button type="button" class="tf-btn{' active' if tf == 60 else ''}" data-tf="60">1h</button>
    <span class="title" style="margin-left:8px">ZOOM</span>
    <button type="button" id="zoomOut" class="tf-btn zoom-btn">−</button>
    <button type="button" id="zoomIn" class="tf-btn zoom-btn">+</button>
    <button type="button" id="zoomReset" class="tf-btn">Full day</button>
    <button type="button" id="dlPng" class="tf-btn" title="Download the chart as a PNG image">⬇ PNG</button>
    <span id="liveCountdown" class="live-countdown"></span>
  </div>
  <canvas id="ch" width="940" height="240"></canvas>
  <p class="marker-note">Red bands = missing candles. Dashed candle = live forming candle.</p>
</div>
<div class="card">
  <p class="panel-title">COMPARE ASSETS — NORMALIZED % CHANGE (INDEXED TO 100)</p>
  <div class="cmp-picker">
    <select id="cmpSelect"></select>
    <button type="button" id="cmpAdd" class="btn ghost">+ Add asset</button>
    <span id="cmpTags"></span>
    <span id="cmpHint" style="color:var(--muted);font-size:12px"></span>
  </div>
  <canvas id="cmp" width="940" height="220"></canvas>
  <div id="cmpLegend" class="legend"></div>
</div>
<div class="card">
  <p class="panel-title">SIGNAL GENERATOR — MAJORITY-VOTE SIGNALS FROM PAST DAYS</p>
  <form id="sigForm" class="sig-form">
    <label>Asset <input id="sigAsset" type="text" value="{html.escape(asset or '')}"></label>
    <label>Date <input id="sigDate" type="date" value="{html.escape(date_str)}"></label>
    <label>Days <input id="sigDays" type="number" min="1" max="10" value="5"></label>
    <label>Win% ≥ <input id="sigPct" type="number" min="0" max="100" value="70"></label>
    <button type="submit" class="btn">Generate</button>
  </form>
  <div id="sigResults" class="sig-list"></div>
</div>
<div class="card table-card">
  <div class="dl-row" style="padding-top:10px">
    <span class="panel-title" style="margin:0">1-MIN CANDLES</span>
    <button type="button" id="dlJson" class="btn ghost">⬇ JSON</button>
    <button type="button" id="dlCsv" class="btn ghost">⬇ CSV</button>
  </div>
  <table><thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Dir</th></tr></thead>
  <tbody id="tbody">{trs}</tbody></table>
  <p id="rowCount" class="row-count">{len(rows)} candles</p>
</div>
<footer>Data: littlematrix-db (read-only) &bull; Auto-refresh every 30s for today &bull; All times Dhaka</footer>
</div>
<script id="init" type="application/json">{init}</script>
<script>{JS}</script>
</body></html>"""


def _viewer_password():
    """The optional password from the environment — never hardcoded,
    never stored anywhere else. Empty/unset = open viewer."""
    return os.environ.get("VIEWER_PASSWORD", "").strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console quiet
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200, extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away — the request is already answered

    def _send_json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json", code)

    def _authorized(self):
        """True when no VIEWER_PASSWORD is set, or the request carries
        HTTP Basic Auth with the env password (any username)."""
        pw = _viewer_password()
        if not pw:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        if ":" not in decoded:
            return False
        _, given = decoded.split(":", 1)
        return hmac.compare_digest(given, pw)

    def do_GET(self):
        # Password gate: EVERY request (pages + API) when set.
        if not self._authorized():
            return self._send(
                "<h2>Password required</h2>"
                "<p>This candle viewer is password protected.</p>",
                code=401,
                extra={"WWW-Authenticate": 'Basic realm="Candle Viewer"'},
            )
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._dispatch(parsed, path)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response — nothing to send
        except Exception as e:
            # Never answer an API call with HTML: the JS does r.json()
            # and an HTML error page breaks every loader. JSON always.
            try:
                if path.startswith("/api/"):
                    self._send_json(
                        {"error": "Database unavailable — retry in a few seconds."},
                        code=500)
                else:
                    self._send(
                        f"<h2>Database error</h2><p>{html.escape(str(e))}</p>",
                        code=500)
            except Exception:
                pass

    def _dispatch(self, parsed, path):
        q = parse_qs(parsed.query)

        def _int(name, default, lo, hi):
            try:
                return max(lo, min(hi, int((q.get(name) or [""])[0] or default)))
            except (TypeError, ValueError):
                return default

        # API endpoints that need no asset list first.
        if path == "/api/live_tick":
            asset = normalize_asset((q.get("asset") or [""])[0] or "")
            return self._send_json(STORE.live_tick(asset))
        if path == "/api/payouts":
            return self._send_json({"payouts": STORE.payouts()})
        if path == "/api/bot_status":
            return self._send_json(STORE.bot_status())
        if path == "/api/best_times":
            asset = normalize_asset((q.get("asset") or [""])[0] or "")
            days = _int("days", 5, 1, 10)
            ref = (q.get("date") or [""])[0] or today_dhaka_str()
            try:
                datetime.strptime(ref, "%Y-%m-%d")
            except ValueError:
                ref = today_dhaka_str()
            return self._send_json(STORE.best_times(asset, days, ref))
        if path == "/api/signals":
            asset = normalize_asset((q.get("asset") or [""])[0] or "")
            date_str = (q.get("date") or [""])[0] or today_dhaka_str()
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                date_str = today_dhaka_str()
            days = _int("days", 5, 1, 10)
            pct = _int("pct", 70, 0, 100)
            gap = (q.get("gap") or ["1"])[0].lower() not in ("0", "false", "no")
            return self._send_json(
                STORE.signals(asset, date_str, days, pct, apply_gap=gap))

        assets = STORE.assets()
        if path == "/api/assets":
            return self._send_json({"assets": assets})
        date_str = (q.get("date") or [""])[0] or today_dhaka_str()
        asset = normalize_asset((q.get("asset") or [""])[0] or (assets[0] if assets else ""))
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            date_str = today_dhaka_str()
        if path == "/api/candles":
            _, rows = STORE.candles(asset, date_str)
            return self._send_json(
                {"asset": asset, "date": date_str, "count": len(rows), "candles": rows})
        if path != "/":
            if path.startswith("/api/"):
                return self._send_json({"error": "unknown endpoint"}, code=404)
            return self._send("<h2>Not found</h2>", code=404)
        tf = _int("tf", 15, 1, 60)
        if tf not in (1, 5, 15, 60):
            tf = 15
        if not assets:
            return self._send(page_html("", date_str, [], [], "No candles in the DB yet.", tf=tf))
        _, rows = STORE.candles(asset, date_str)
        err = "" if rows else "No candles for this asset on this date — try another date."
        self._send(page_html(asset, date_str, assets, rows, err, tf=tf))

def main():
    global STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--demo", action="store_true",
                    help="fake sample data, no DATABASE_URL needed")
    args = ap.parse_args()
    if args.demo:
        STORE = Store(connect_demo(), demo=True)
        print("Demo mode — fake sample data only, not your real DB.")
    else:
        STORE = Store(connect_real())
        print(f"Connected. Assets in DB: {len(STORE.assets())}")
    if _viewer_password():
        print("Password protection is ON (VIEWER_PASSWORD is set).")
    else:
        print("Password protection is OFF (VIEWER_PASSWORD is not set).")
    # Render sets PORT and expects 0.0.0.0; on your PC it stays local.
    port = int(os.environ.get("PORT", args.port))
    host = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    if host == "127.0.0.1":
        print(f"Open in your browser: http://127.0.0.1:{port}/")
        print("Press Ctrl+C to stop. (Only your PC can open it — it is not public.)")
    else:
        print(f"Listening on {host}:{port} — open the Render service URL.")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
