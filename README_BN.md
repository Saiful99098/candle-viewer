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
- **ZOOM:** চার্টের উপরে mouse wheel ঘোরালে zoom in/out হবে (cursor যেখানে সেখানেই zoom হবে), চার্ট ধরে টানলে (drag) ডানে-বামে সরানো যাবে। ফোনে **+ / −** বাটন চাপলে zoom হবে, **Full day** চাপলে আবার পুরো দিন দেখাবে। নতুন candle আসলে zoom আর reset হয় না — live edge-এ থাকলে view নিজে নিজে ডানে সরে যাবে যাতে latest candle দেখা যায়, আর পুরনো candle দেখতে থাকলে view যেখানে ছিল সেখানেই থাকবে
- **Header:** উপরে asset-এর নাম বড় করে, live price বড় করে, আর দিনের open থেকে কত % বদলেছে (সবুজ/লাল) দেখাবে। পাশে ওই asset-এর payout % ও দেখাবে
- **EMA + RSI:** চার্টের উপরে EMA 9 (সায়ান) আর EMA 21 (বেগুনি) লাইন, আর চার্টের নিচে RSI 14 প্যানেল (70/30 লাইন সহ) — বটের `signal_engine.py`-র `ema`/`rsi` ফর্মুলা হুবহু একই
- **Best-time marker:** গত ৫ দিনের ডেটা থেকে যে HH:MM সময়ে win% ৭০% বা তার বেশি, সেগুলো চার্টে ▲ CALL / ▼ PUT চিহ্ন দিয়ে দেখাবে। নিচে **BEST TIME SLOTS** প্যানেলে সেরা ১০টা সময়ের list আছে (win%, CALL/PUT ভোট সহ)
- **Gap লাল চিহ্ন:** যেখানে candle missing, চার্টে লাল band আর টেবিলে লাল GAP row দেখাবে
- **COMPARE:** সর্বোচ্চ ৩টা asset বেছে একসাথে compare করো — normalized % change লাইন (১০০ থেকে শুরু), legend-এ প্রতিটার % change লেখা থাকবে
- **Download:** টেবিলের উপরে ⬇ JSON / ⬇ CSV বাটন — বর্তমান asset+date-র ডেটা ডাউনলোড হবে। চার্টের ⬇ PNG বাটন — চার্টের ছবি (PNG) ডাউনলোড হবে
- **LIVE candle (Fix 11):** বট live tick পাঠালে প্রতি সেকেন্ডে আপডেট হওয়া forming candle চার্টে দেখাবে (dashed outline + LIVE লেখা), সাথে candle close হতে কত সেকেন্ড বাকি তার countdown, আর ১০ সেকেন্ড tick না আসলে STALE badge। বট এখনো tick না পাঠালে ভিউয়ার আগের মতোই চলবে — live candle শুধু লুকানো থাকবে
- **Payout:** search list, quick chip আর header-এ প্রতিটা asset-এর payout % দেখাবে (বটের `payouts` টেবিল থেকে)
- **BOT STATUS:** পেজের উপরে বটের status bar — last/next collection, live stream state, Stopped/Resumed asset, Quotex session, free/premium ON/OFF
- **SIGNAL GENERATOR:** asset + date + days + win% দিয়ে ভিউয়ার থেকেই signal list বানাও — বটের majority-vote লজিক (৫ দিনের HH:MM ভোট, strength filter, ৭ মিনিট gap)
- JSON হিসেবে চাইলে: `https://blue-fish-candle-viewer.onrender.com/api/candles?asset=BTCUSD_otc&date=2026-11-26`
- আরও API: `/api/best_times?asset=...&days=5`, `/api/signals?asset=...&date=...&days=5&pct=70`, `/api/payouts`, `/api/live_tick?asset=...`, `/api/bot_status`, `/api/assets`

## ৪. পাসওয়ার্ড সুরক্ষা (ঐচ্ছিক)

ভিউয়ার এখন সবার জন্য খোলা। চাইলে পাসওয়ার্ড দিতে পারো — তাহলে পেজ আর API খুলতে পাসওয়ার্ড লাগবে:

১. Render Dashboard → `blue-fish-candle-viewer` সার্ভিস → **Environment**
২. **Add Environment Variable** — Key: `VIEWER_PASSWORD`, Value: তোমার পছন্দের পাসওয়ার্ড (যেকোনো একটা দাও, কাউকে পাঠিও না)
৩. Save করলেই সার্ভিস restart হবে — এরপর লিংক খুললে ব্রাউজার username/password চাইবে (username যেকোনো কিছু, password তোমার দেওয়া `VIEWER_PASSWORD`)
৪. পাসওয়ার্ড সরাতে চাইলে Render থেকে `VIEWER_PASSWORD` variable-টা delete করো — আবার খোলা হয়ে যাবে

মনে রাখো: পাসওয়ার্ড কোডে বা GitHub-এ কখনো লিখো না — শুধু Render-এর Environment Variable-এ দাও।

## মনে রাখো

- Free plan-এ ১৫ মিনিট কেউ না খুললে সার্ভিস ঘুমিয়ে যায় — লিংক খুললেই আবার জেগে উঠবে, ২০-৩০ সেকেন্ড লাগতে পারে
- এই লিংক যার কাছে থাকবে, সে-ই candle ডেটা দেখতে পারবে — এতে শুধু বাজারের ডেটা আছে, কোনো পাসওয়ার্ড নেই
- PC-তে লোকালভাবে চালাতে চাইলে: `export DATABASE_URL="postgres://..."` দিয়ে `python view_candles.py` চালাও, তারপর http://127.0.0.1:8001/ খোলো
