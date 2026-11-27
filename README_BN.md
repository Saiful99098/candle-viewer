# Candle Viewer — অনলাইনে দেখার নিয়ম

এই ভিউয়ার শুধু ডেটাবেজ থেকে candle পড়ে, কিছু লেখে না। বট যেমন চলছে তেমনই চলবে।

## ১. GitHub-এ পাঠাও

১. GitHub-এ নতুন repo বানাও, নাম: `candle-viewer`
২. এই ফোল্ডারের ৪টা ফাইল (`view_candles.py`, `requirements.txt`, `render.yaml`, `README_BN.md`) ওই repo-তে push করো
৩. `.env` বা `DATABASE_URL` — কিছুই GitHub-এ দিও না

## ২. Render-এ চালু করো

১. Render Dashboard → **New +** → **Web Service** → `candle-viewer` repo বেছে নাও
২. নাম দাও: `blue-fish-candle-viewer`, Region: Singapore, Plan: **Free**
৩. **Environment**-এ যাও → **Add Environment Variable**
   - Key: `DATABASE_URL`
   - Value: Render Dashboard → `littlematrix-db` → Info → **External Database URL** (পুরোটা, কাউকে পাঠিও না)
৪. **Create Web Service** চাপো — ২-৩ মিনিটে deploy হবে
৫. উপরে যে URL দেবে, সেটাই তোমার ভিউয়ার:
   `https://blue-fish-candle-viewer.onrender.com/`

## ৩. ব্যবহার

- পেজ খুলে search box-এ asset-এর নাম লিখো (যেমন EURUSD) — নিচে list আসবে, বেছে নাও; অথবা Quick chip-এ চাপ দাও। date দাও → পুরো দিনের candle টেবিল + চার্ট আসবে (ঢাকা সময়)
- **Auto-refresh:** আজকের date হলে প্রতি ৩০ সেকেন্ডে নিজে নিজে নতুন candle আনবে — reload করতে হবে না। বন্ধ করতে চাইলে Auto-refresh টিক চিহ্ন তুলে দাও
- **LIVE badge:** last candle ২ মিনিটের মধ্যে হলে সবুজ LIVE দেখাবে, পুরনো হলে STALE/DELAYED দেখাবে (কত মিনিট আগের সেটাও লেখা থাকবে)
- **CANDLE CHART:** লাইন চার্টের বদলে এখন আসল candlestick চার্ট — সবুজ/লাল candle, wick সহ। উপরে 1m / 5m / 15m / 1h বাটন আছে — পুরো দিন দেখতে 15m বা 1h ভালো, 1m-এ প্রতিটা মিনিটের candle দেখাবে (নিচের টেবিল সবসময় 1-min candle-ই দেখায়)
- JSON হিসেবে চাইলে: `https://blue-fish-candle-viewer.onrender.com/api/candles?asset=BTCUSD_otc&date=2026-11-26`

## মনে রাখো

- Free plan-এ ১৫ মিনিট কেউ না খুললে সার্ভিস ঘুমিয়ে যায় — লিংক খুললেই আবার জেগে উঠবে, ২০-৩০ সেকেন্ড লাগতে পারে
- এই লিংক যার কাছে থাকবে, সে-ই candle ডেটা দেখতে পারবে — এতে শুধু বাজারের ডেটা আছে, কোনো পাসওয়ার্ড নেই
- PC-তে লোকালভাবে চালাতে চাইলে: `export DATABASE_URL="postgres://..."` দিয়ে `python view_candles.py` চালাও, তারপর http://127.0.0.1:8001/ খোলো
