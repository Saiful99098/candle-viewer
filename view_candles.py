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

UI: dark-ash premium theme, asset search, auto-refresh every 30s
for today, live/stale badge, and a gap detector in the stats.
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
  --bg:#16161a; --panel:#222228; --panel2:#2b2b33; --border:#3a3a44;
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
.search input,.controls input[type=date]{width:100%;background:#17171b;border:1px solid var(--border);
  color:var(--text);border-radius:10px;padding:11px 12px;font-size:15px;outline:none}
.search input:focus,.controls input[type=date]:focus{border-color:var(--cyan)}
.asset-list{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:20;display:none;
  background:var(--panel2);border:1px solid var(--border);border-radius:10px;overflow:hidden;
  max-height:320px;overflow-y:auto;box-shadow:0 12px 30px rgba(0,0,0,.5)}
.match{padding:10px 12px;cursor:pointer;font-family:ui-monospace,Menlo,monospace;font-size:14px}
.match:hover{background:rgba(139,92,246,.18)}
.match.empty{color:var(--muted);cursor:default}
.match-count{padding:8px 12px;color:var(--muted);font-size:12px;border-top:1px solid var(--border);
  position:sticky;bottom:0;background:var(--panel2)}
.controls button{background:linear-gradient(135deg,var(--violet),var(--cyan));color:#fff;border:0;
  border-radius:10px;padding:11px 22px;font-size:15px;font-weight:700;cursor:pointer}
.controls button:hover{filter:brightness(1.12)}
.auto-toggle{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer;white-space:nowrap}
.auto-toggle input{accent-color:var(--violet);width:16px;height:16px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 2px 12px;align-items:center;color:var(--muted);font-size:13px}
.chip{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:999px;
  padding:6px 12px;font-size:13px;cursor:pointer;font-family:ui-monospace,Menlo,monospace}
.chip:hover{border-color:var(--cyan);color:#67e8f9}
.err{color:#fb7185;margin:0 2px 12px;font-size:14px}
.err:empty{display:none}
.asset-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.asset-name{font-size:22px;font-weight:800;font-family:ui-monospace,Menlo,monospace}
.asset-date{color:var(--muted);font-size:13px;margin-top:2px}
.asset-head-right{display:flex;align-items:center;gap:12px}
.badge{padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.5px}
.badge.live{background:rgba(34,197,94,.18);color:#4ade80;animation:pulse 1.6s infinite}
.badge.delayed{background:rgba(245,158,11,.18);color:#fbbf24}
.badge.stale{background:rgba(244,63,94,.18);color:#fb7185}
.badge.hist{background:rgba(255,255,255,.08);color:var(--muted)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.last-price{font-family:ui-monospace,Menlo,monospace;font-size:19px;font-weight:700}
.last-price.up{color:#4ade80}
.last-price.down{color:#fb7185}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.stat .label{color:var(--muted);font-size:11px;letter-spacing:1px;text-transform:uppercase}
.stat .value{font-size:20px;font-weight:700;margin-top:4px;font-family:ui-monospace,Menlo,monospace}
.status-line{display:flex;gap:16px;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:13px}
.status-line .ok{color:#4ade80}
.status-line .warn{color:#fbbf24}
.auto-progress{width:120px;height:6px;border-radius:99px;background:#33333c;overflow:hidden;display:inline-block;vertical-align:middle}
.auto-progress span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--violet),var(--cyan))}
.chart-card{padding:10px 12px}
.chart-toolbar{display:flex;gap:8px;align-items:center;margin:2px 4px 10px;flex-wrap:wrap}
.chart-toolbar .title{font-weight:700;font-size:13px;letter-spacing:1px;color:var(--muted);margin-right:4px}
.tf-btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:999px;
  padding:5px 13px;font-size:13px;cursor:pointer;font-weight:700}
.tf-btn:hover{border-color:var(--cyan)}
.tf-btn.active{background:linear-gradient(135deg,var(--violet),var(--cyan));border-color:transparent;color:#fff}
canvas{width:100%;height:240px;display:block}
.table-card{padding:6px 0 10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:560px}
td,th{padding:7px 12px;text-align:right;font-family:ui-monospace,Menlo,monospace;font-size:13px;border-bottom:1px solid #2e2e36}
td:first-child,th:first-child{text-align:left}
thead th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-size:11px;letter-spacing:1px;text-transform:uppercase}
tbody tr:nth-child(even){background:rgba(255,255,255,.02)}
tbody tr:hover{background:rgba(139,92,246,.08)}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700}
.pill.call{background:rgba(34,197,94,.15);color:#4ade80}
.pill.put{background:rgba(244,63,94,.15);color:#fb7185}
.pill.doji{background:rgba(245,158,11,.15);color:#fbbf24}
.empty{color:var(--muted);text-align:center !important;padding:18px !important}
.row-count{color:var(--muted);font-size:12px;margin:10px 14px 0}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:18px;line-height:1.7}
@media (max-width:640px){
  .brand h1{font-size:17px}
  .asset-name{font-size:18px}
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
  return {time: time, date: date, min: (+time.slice(0, 2)) * 60 + (+time.slice(3, 5))};
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

let tf = 15;  // chart timeframe in minutes (1 / 5 / 15 / 60)

function aggregate(rows, tfMin){
  // Group 1-min rows into tfMin-minute candles, aligned to the day:
  // open = first open, high = max high, low = min low, close = last close.
  // The last (in-progress) bucket is included — it is the live candle.
  if (tfMin <= 1) return rows;
  const out = [];
  let curB = null;
  rows.forEach(r => {
    const b = Math.floor(toMin(r.time) / tfMin);
    if (!curB || curB.bucket !== b){
      curB = {bucket: b, time: r.time, open: r.open, high: r.high, low: r.low, close: r.close};
      out.push(curB);
    } else {
      if (r.high > curB.high) curB.high = r.high;
      if (r.low < curB.low) curB.low = r.low;
      curB.close = r.close;
    }
  });
  return out;
}

function drawChart(rows){
  const cv = $('ch'), ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++){ const y = H * i / 4; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  const candles = aggregate(rows, tf);
  if (candles.length < 2){
    ctx.fillStyle = '#9a9ca8'; ctx.font = '14px system-ui';
    ctx.fillText('No data to chart', 16, H / 2);
    return;
  }
  let mn = Infinity, mx = -Infinity;
  candles.forEach(c => { if (c.low < mn) mn = c.low; if (c.high > mx) mx = c.high; });
  const rg = (mx - mn) || 1;
  const padL = 8, padR = 8, padT = 14, padB = 22;
  const slotW = (W - padL - padR) / candles.length;
  const bodyW = Math.max(1, Math.floor(slotW * 0.7));
  const X = i => padL + i * slotW + slotW / 2;
  const Y = v => padT + (1 - (v - mn) / rg) * (H - padT - padB);
  candles.forEach((c, i) => {
    const col = c.close > c.open ? '#22c55e' : (c.close < c.open ? '#f43f5e' : '#f59e0b');
    const x = X(i);
    // wick: high to low
    ctx.strokeStyle = col; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, Y(c.high)); ctx.lineTo(x, Y(c.low)); ctx.stroke();
    // body: open to close (doji = a flat line)
    const yO = Y(c.open), yC = Y(c.close);
    ctx.fillStyle = col;
    if (Math.abs(yO - yC) < 1){
      ctx.fillRect(x - bodyW / 2, yO - 0.5, bodyW, 1);
    } else {
      ctx.fillRect(x - bodyW / 2, Math.min(yO, yC), bodyW, Math.abs(yO - yC));
    }
  });
  const last = candles[candles.length - 1];
  const lastCol = last.close > last.open ? '#22c55e' : (last.close < last.open ? '#f43f5e' : '#f59e0b');
  ctx.fillStyle = '#9a9ca8'; ctx.font = '12px system-ui';
  ctx.fillText(candles[0].time, 10, H - 6);
  ctx.fillText(last.time, W - 46, H - 6);
  ctx.fillStyle = lastCol; ctx.font = 'bold 13px system-ui';
  ctx.fillText(fmtPrice(last.close), Math.max(10, X(candles.length - 1) - 70), Math.max(16, Y(last.close) - 10));
}

document.querySelectorAll('.tf-btn').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.tf-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    tf = parseInt(b.dataset.tf, 10) || 15;
    drawChart(cur.rows);
  });
});

function renderAll(){
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
  const st = statusOf(rows);
  const b = $('liveBadge'); b.className = 'badge ' + st.cls; b.textContent = st.label;
  $('gapsLine').innerHTML = 'Gaps: ' + gapsText(computeGaps(rows));
  $('assetTitle').textContent = cur.asset;
  $('dateTitle').textContent = cur.date + ' (Dhaka time)';
  const lastP = rows.length ? rows[rows.length - 1].close : null;
  const firstO = rows.length ? rows[0].open : null;
  const lp = $('lastPrice');
  if (lastP !== null){
    lp.textContent = (lastP >= firstO ? '▲ ' : '▼ ') + fmtPrice(lastP);
    lp.className = 'last-price ' + (lastP >= firstO ? 'up' : 'down');
  } else { lp.textContent = '—'; lp.className = 'last-price'; }
  let h = '';
  rows.forEach(r => {
    h += '<tr><td>' + r.time + '</td><td>' + fmtPrice(r.open) + '</td><td>' + fmtPrice(r.high) +
      '</td><td>' + fmtPrice(r.low) + '</td><td>' + fmtPrice(r.close) + '</td><td>' + pillOf(r) + '</td></tr>';
  });
  $('tbody').innerHTML = h || '<tr><td colspan="6" class="empty">No candles for this asset on this date.</td></tr>';
  $('rowCount').textContent = rows.length + ' candles';
  drawChart(rows);
}

async function loadAsset(asset, date){
  asset = (asset || '').trim();
  if (!asset) return;
  const url = '/api/candles?asset=' + encodeURIComponent(asset) + '&date=' + encodeURIComponent(date || cur.date);
  try {
    const r = await fetch(url);
    const j = await r.json();
    cur.asset = j.asset; cur.date = j.date; cur.rows = j.candles || [];
    document.title = 'Candles — ' + cur.asset + ' ' + cur.date;
    history.replaceState(null, '', '/?asset=' + encodeURIComponent(cur.asset) + '&date=' + encodeURIComponent(cur.date));
    $('dateInput').value = cur.date;
    $('errLine').textContent = cur.rows.length ? '' : 'No candles for this asset on this date — try another date.';
    renderAll();
    setUpdated();
    countdown = 30;
  } catch (e){
    $('errLine').textContent = 'Could not load candles — check connection and retry.';
  }
}

function setUpdated(){ $('updatedAt').textContent = 'Updated ' + dhakaNow().time + ' (Dhaka)'; }
function refreshIfToday(){
  if (!autoOn) return;
  if (cur.date !== dhakaNow().date) return;
  loadAsset(cur.asset, cur.date);
}

const search = $('assetSearch'), list = $('assetList');
function showMatches(q){
  q = (q || '').trim().toUpperCase();
  const m = ASSETS.filter(a => !q || a.toUpperCase().includes(q));
  if (!m.length){ list.innerHTML = '<div class="match empty">No asset matches.</div>'; list.style.display = 'block'; return; }
  list.innerHTML = m.map(a => '<div class="match" data-a="' + a + '">' + a + '</div>').join('') +
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
renderAll();
setUpdated();
$('clock').textContent = dhakaNow().time + ' Dhaka';
"""


def page_html(asset, date_str, assets, rows, error=""):
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
  </div>
  <div class="asset-head-right">
    <span id="liveBadge" class="badge hist">—</span>
    <span id="lastPrice" class="last-price">—</span>
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
<div class="card chart-card">
  <div class="chart-toolbar"><span class="title">CANDLE CHART</span>
    <button type="button" class="tf-btn" data-tf="1">1m</button>
    <button type="button" class="tf-btn" data-tf="5">5m</button>
    <button type="button" class="tf-btn active" data-tf="15">15m</button>
    <button type="button" class="tf-btn" data-tf="60">1h</button>
  </div>
  <canvas id="ch" width="940" height="240"></canvas>
</div>
<div class="card table-card">
  <table><thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Dir</th></tr></thead>
  <tbody id="tbody">{trs}</tbody></table>
  <p id="rowCount" class="row-count">{len(rows)} candles</p>
</div>
<footer>Data: littlematrix-db (read-only) &bull; Auto-refresh every 30s for today &bull; All times Dhaka</footer>
</div>
<script id="init" type="application/json">{init}</script>
<script>{JS}</script>
</body></html>"""


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
