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
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
    DHAKA = ZoneInfo("Asia/Dhaka")
except Exception:  # pragma: no cover
    DHAKA = timezone(timedelta(hours=6))
UTC = timezone.utc

DEFAULT_PORT = 8001


def normalize_asset(asset):
    """Same rule as the bot: BTCUSD-OTC -> BTCUSD_otc."""
    a = (asset or "").strip().upper().replace("-", "_")
    if a.endswith("_OTC"):
        a = a[:-4] + "_otc"
    return a


def dhaka_date_range_utc(date_str):
    """Full Dhaka day -> half-open UTC range [start, end)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, tzinfo=DHAKA).astimezone(UTC)
    return start, start + timedelta(days=1)


def today_dhaka_str():
    return datetime.now(DHAKA).strftime("%Y-%m-%d")


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
    """Fake in-memory SQLite DB with sample candles — UI test only."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE candles (asset TEXT, ts TIMESTAMP, open REAL, "
        "high REAL, low REAL, close REAL, UNIQUE(asset, ts))"
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
    conn.commit()
    return conn


class Store:
    """One shared DB connection + the two queries the page needs."""

    def __init__(self, conn, demo=False):
        self.conn = conn
        self.demo = demo
        self.ph = "?" if demo else "%s"

    def assets(self):
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT asset FROM candles ORDER BY asset")
        return [r[0] for r in cur.fetchall()]

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
            if isinstance(ts, datetime):
                dt = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
            else:
                dt = datetime.strptime(str(ts), fmt).replace(tzinfo=UTC)
            rows.append({
                "time": dt.astimezone(DHAKA).strftime("%H:%M"),
                "open": float(o), "high": float(h),
                "low": float(l), "close": float(c),
            })
        return asset, rows


STORE = None  # set in main()


# -------------------------------------------------------------- HTML ----
def page_html(asset, date_str, assets, rows, error=""):
    opts = "".join(
        f'<option value="{html.escape(a)}"'
        f'{" selected" if a == asset else ""}>{html.escape(a)}</option>'
        for a in assets
    )
    trs = "".join(
        f"<tr><td>{r['time']}</td><td>{r['open']:.5f}</td>"
        f"<td>{r['high']:.5f}</td><td>{r['low']:.5f}</td>"
        f"<td>{r['close']:.5f}</td>"
        f"<td>{'CALL' if r['close'] > r['open'] else 'PUT' if r['close'] < r['open'] else 'DOJI'}</td></tr>"
        for r in rows
    )
    data = json.dumps([r["close"] for r in rows])
    labels = json.dumps([r["time"] for r in rows])
    err = f'<p style="color:#b00">{html.escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Candles — {html.escape(asset or '')} {html.escape(date_str)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:20px auto;padding:0 12px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:right;font-family:monospace}}td:first-child,th:first-child{{text-align:left}}select,input,button{{padding:6px;font-size:15px}}canvas{{width:100%;height:220px;border:1px solid #ddd}}</style>
</head><body>
<h2>BLUE FISH PRIME — Candle viewer</h2>
<form method="get"><select name="asset">{opts}</select>
<input type="date" name="date" value="{html.escape(date_str)}">
<button type="submit">Show</button></form>
{err}
<p><b>{html.escape(asset or '')}</b> — {html.escape(date_str)} (Dhaka time) — <b>{len(rows)}</b> candles</p>
<canvas id="ch" width="880" height="220"></canvas>
<table><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Dir</th></tr>{trs}</table>
<script>
const data={data}, labels={labels};
const cv=document.getElementById('ch'), ctx=cv.getContext('2d');
if(data.length>1){{const mn=Math.min(...data), mx=Math.max(...data), rg=(mx-mn)||1;
ctx.beginPath(); data.forEach((v,i)=>{{const x=10+i*(860/(data.length-1)), y=200-((v-mn)/rg)*180; i?ctx.lineTo(x,y):ctx.moveTo(x,y);}}); ctx.strokeStyle='#2563eb'; ctx.lineWidth=2; ctx.stroke();
ctx.fillStyle='#555'; ctx.fillText(labels[0]||'',10,215); ctx.fillText(labels[labels.length-1]||'',800,215);}}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console quiet
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        try:
            assets = STORE.assets()
        except Exception as e:  # DB went away mid-run
            return self._send(f"<h2>Database error</h2><p>{html.escape(str(e))}</p>", code=500)
        if path == "/api/assets":
            return self._send(json.dumps({"assets": assets}), "application/json")
        date_str = (q.get("date") or [""])[0] or today_dhaka_str()
        asset = normalize_asset((q.get("asset") or [""])[0] or (assets[0] if assets else ""))
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            date_str = today_dhaka_str()
        if path == "/api/candles":
            _, rows = STORE.candles(asset, date_str)
            return self._send(json.dumps(
                {"asset": asset, "date": date_str, "count": len(rows), "candles": rows}),
                "application/json")
        if path != "/":
            return self._send("<h2>Not found</h2>", code=404)
        if not assets:
            return self._send(page_html("", date_str, [], [], "No candles in the DB yet."))
        _, rows = STORE.candles(asset, date_str)
        err = "" if rows else "No candles for this asset on this date — try another date."
        self._send(page_html(asset, date_str, assets, rows, err))


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
    # Render sets PORT and expects 0.0.0.0; on your PC it stays local.
    port = int(os.environ.get("PORT", args.port))
    host = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    if host == "127.0.0.1":
        print(f"Open in your browser: http://127.0.0.1:{port}/")
        print("Press Ctrl+C to stop. (Only your PC can open it — it is not public.)")
    else:
        print(f"Listening on {host}:{port} — open the Render service URL.")
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
