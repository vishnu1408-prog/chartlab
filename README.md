# ChartLab — local chart + bar-replay practice terminal

A TradingView-style chart that runs on your own machine: NSE indices and 118 stocks, your own indicators,
drawing tools, and **bar replay** (rewind to any day and practise trading bar by bar).

Open **http://localhost:8050** after starting it.

## Start / stop (Docker)

```bash
cd ~/chartlab
./chartlab.sh build      # build the image (runs all backend tests; a failing test stops the build)
./chartlab.sh start      # start in the background, restarts automatically after a reboot
./chartlab.sh status     # running? which data source?
./chartlab.sh logs       # follow the server log
./chartlab.sh stop
./chartlab.sh test       # run the tests outside Docker (Python + Node)
./chartlab.sh dev        # run without Docker with auto-reload (for editing the code)
```

The container only listens on `127.0.0.1:8050` (not reachable from other machines).

## Data

| Source | What | Notes |
|---|---|---|
| Kaggle (already on this PC) | 1-minute history for ~115 NSE indices (2015 → 15 May 2026) and 118 stocks (→ Apr 2026) | mounted read-only |
| Hugging Face `thetrademarkk/india-index-options-1m` (CC BY-NC 4.0, educational use) | 1-minute spot NIFTY / BANKNIFTY (May 2021 → 2 Jul 2026), SENSEX (Sep 2022 → 2 Jul 2026); **real 1-minute option prices** per weekly expiry | spot files in `data/hf/index/`; option files are downloaded per expiry the first time you trade it (`data/hf/options/`) |
| **Angel One SmartAPI** | fills the gap from the last stored bar to today in 1-minute bars + live bars every 5 s | used automatically once `~/.config/angel/creds.env` is filled in. Market data only — never places orders |
| Yahoo Finance (fallback) | 1-minute (7 days), 5-minute (60 days), hourly (2 years), daily (full history, SENSEX since 1997) | ~1 minute delayed. Coarser data only fills days that have nothing finer |
| **Binance** public data (no account, no key) | **BTCUSD**: 1-minute BTCUSDT history from Jan 2021 (monthly archives in `data/binance/`) + live bars every 5 s (Angel's quote feed builds the current minute; closed minutes are re-checked against Angel's candles once a minute); daily from 2014 via Yahoo | change `BINANCE_FROM` in `backend/app/sources.py` for more history (≈1.5M rows a year, uses RAM) |
| **Angel One** (MCX) | **MCX GOLD, MCX SILVER, MCX CRUDEOIL, MCX NATURALGAS**: continuous near-month futures in ₹ — daily from 2012, 1-minute and 5-minute from 2016 (Angel has no intraday before 2016; 2022 daily and gold 2024–25 intraday have gaps on Angel's side) + live bars every 5 s (Angel's quote feed builds the current minute; closed minutes are re-checked against Angel's candles once a minute) | listed once Angel creds are filled in. The history downloads in the background the first time (newest first; Angel's candle quota is tight — often only ~2 requests a minute — so the full 1-minute history can take days; it **pauses 09:00–15:35 on weekdays** so live NSE charts keep the quota; the status line shows progress, a restart resumes). Files: `data/angel_hist/<symbol>/1m_<year>.parquet`. Small price jumps on contract-roll days |
| Yahoo `GC=F` | **XAUUSD**: COMEX gold futures (a few $ above spot) — 1-minute 7 days, 5-minute 60 days, hourly 2 years, daily since 2000 | 1-minute history grows each time it syncs; Yahoo has no spot gold |

BTCUSD and XAUUSD trade ~24 hours. Their days run 05:30 → 05:30 IST (00:00 UTC, as TradingView shows them in IST), hourly
bars start at :30, replay starts at 05:30, and practice trades square off at 05:15 IST. NSE indicators (CPR, sideways
meter, 15:15 harmonic rules) still assume NSE hours, so read them with care on these two.

MCX commodities: the day starts at 09:00 IST (bars anchored there; 1-hour bars at 09:00, 10:00 …), the session runs to
23:30 (23:55 in the northern winter), replay starts at 09:00 and practice trades square off at **23:00**. The practice
lot is the mini contract's multiplier (₹ per point): GOLDM ×10 (100 g), SILVERM ×5 (5 kg), CRUDEOILM ×10, NATGASMINI ×250.
Same caveat: NSE-hours indicators (CPR, fake-breakout checker, sideways meter) were built for NIFTY.

Click **⟳ Sync** to fetch the newest bars for the current symbol (it also happens automatically when you open one).

## Using it

- **Split screen**: ▢ ◫ ⊟ ⊞ in the top bar = 1, 2 side by side, 2 stacked or 4 chart windows. Each window has its own
  symbol, timeframe, indicators and drawings (a new window opens INDIA VIX, then NIFTY BANK, SENSEX). Click a window (or
  press `Tab`) to select it — blue border; the symbol box, timeframe buttons, ƒx Indicators and drawing tools act on the
  selected window. 🔗 links the windows: crosshair and time scrolling move together. The layout is remembered.
- **Symbol**: type in the box (or press `/`); part of a name is enough. **Timeframe**: buttons or keys `1`–`7` (1m 3m 5m 15m 30m 1h 1D).
  Scroll left to load older history.
- **ƒx Indicators**: EMA, SMA, Bollinger, RSI, MACD, Supertrend, VWAP (stocks), EMA crossover, CPR/pivots + previous-day
  high/low, Opening Range, previous-day volume profile (POC/VAH/VAL) and **Harmonic Patterns** (the same engine as
  `harmonic_patterns.pine`). ⚙ in the legend opens the settings, 👁 hides, ✕ removes.
- **Drawing tools** (left bar): trend line, ray, horizontal / vertical line, rectangle, Fibonacci, long/short position
  (risk:reward), measure, text, and the **⋀⋁ Patterns** menu (like TradingView's): XABCD, Cypher, Head and shoulders
  (with neckline), ABCD, Triangle, Three drives (all with live Fibonacci ratios), Elliott impulse (1-2-3-4-5),
  correction (A-B-C), triangle (A-B-C-D-E), double combo (W-X-Y), triple combo (W-X-Y-X-Z), and cycles: cyclic lines,
  time cycles, sine line. 🧲 magnet snaps to O/H/L/C. Click a drawing to select →
  drag to move, drag a handle to reshape, change colour/width/text in the box at the top, `Del` to delete.
- **🕯 Chart type: Candles ↔ Heikin Ashi** (top bar button or `Alt+K`, saved per window). Only the drawing changes: the
  price axis keeps a dotted line at the **real** last price, the legend shows the HA values plus `· price` (real close),
  practice orders, stops / targets, indicators and the volume profile all use the real OHLC (TradingView computes
  indicators on HA data by default — ChartLab does not). HA bars use only past bars, so replay is safe; in minute replay
  the forming HA candle is rebuilt from the previous HA bar each minute. Backtest note (tests/color_candle.py): trading
  HA colour flips on NIFTY lost money on every timeframe — use HA to read the trend, not as a signal.
- **▤ Fixed range volume profile** (like TradingView's FRVP): click the start and the end of a range → histogram of
  volume per price row (green = up candles, red = down), POC (busiest price, orange), value area (70%, bright rows) with
  VAH / VAL lines. "Rows" in the edit box sets the resolution. Indices have no volume, so for NIFTY / SENSEX / indices it
  is a **time-at-price** profile (how many bars traded at each price) — the label says which. In replay it only uses
  the candles revealed so far, so it never shows the future.
  Drawings are saved per symbol and work on every timeframe.
- **⏪ Replay**: click it, then click the bar where you want to start (or pick a date / 🎲 random day in the replay bar).
  - **1 minute steps** (default): the current candle builds up minute by minute, like live — on any timeframe.
    `↓` / ⇥ jumps to the candle's close. Falls back to whole bars on days without 1-minute data.
  - **1 bar steps**: each step reveals a whole candle.
  - `→` forward, `←` back (rewind), `Shift` = 10 steps, `Space` play/pause, speed 1–20 steps/s. More data loads
    automatically when you reach the end. Changing timeframe or step mode keeps the same moment.
  - **No look-ahead**: future bars are hidden, and indicator values, markers and patterns appear only once their bar
    has closed.
  - **Split screen in replay**: the window you start from is the main one (you step it, and practice trades use it);
    every other window follows the same clock — e.g. NIFTY 5m on the left and INDIA VIX on the right always show the
    same minute. You can change a window's symbol or timeframe during the replay; it reloads at the same moment.
- **Practice trading** (right panel): BUY / SELL / Close (or `Shift+B / S / C`) at the last revealed price ± slippage,
  with stop and target in points. Stop beats target inside one bar; gaps fill at the open. Intraday rules: automatic
  square-off at 15:15 and no new trades after it. Every closed trade goes to the **Journal**.
  - **ATM Option** mode (NIFTY 50, NIFTY BANK, SENSEX, in replay): BUY CE / BUY PE buys the at-the-money weekly option.
    It uses **real 1-minute option prices** when the dataset has them (≈ the last 8 trading days before each expiry,
    2021 → 2026), otherwise a Black-Scholes estimate (India VIX volatility, real time to expiry → theta). The position
    box says which. Stop / target are in option points; P&L = option points × quantity − costs.

## ⛓ Option chain (side panel → Options)

Live chain from Angel One quotes (market data only) for **NIFTY 50, NIFTY BANK, NIFTY FIN SERVICE, SENSEX, MCX GOLD,
SILVER, CRUDEOIL, NATURALGAS** — follows the active chart's symbol. Expiry picker, ±5…±12 strikes around the money,
per strike: OI (bar), ΔOI, IV, LTP + change, bid / ask on hover; header: underlying (index, or for MCX the futures
contract the options settle into), ATM straddle, PCR of the strikes shown, days left, lot. Refreshes every 5 s while the
market is open (one quote request). Notes: OI / volume in **lots**; ΔOI counts from the first time ChartLab saw the strike
today (Angel gives no previous-day OI); IV is solved from the last price (Black, zero rate) — an estimate. Live only (not
in replay). `GET /api/chain?symbol=&expiry=&n=` · code: `backend/app/chain.py`, `frontend/js/chain.js`.

Every chart also shows the **time left until the current candle closes** under the last price on the price axis (all
timeframes; a candle never runs past the session close; MCX closes 23:30 in US summer time, 23:55 in winter).

## Practice tools

- **🛡 Rules** (practice panel): your discipline rules — max trades a day, daily loss limit (auto-closes the position and
  stops you for the day), no entries before / after a time, stop loss required, max quantity, cool-down after a loss,
  stop after N losses in a row. Breaking orders are blocked and counted.
- **Setup tag + note** on every trade (Harmonic, Breakout, Pullback…), saved in the journal.
- **📊 Stats**: win rate, net ₹, average win / loss, profit factor, drawdown, best / worst day, equity curve, and P&L by
  setup, entry hour, weekday, instrument (index / CE / PE) and exit reason. ▶ on any trade replays it on the chart.
- Your entries / exits are marked on the chart; drag the SL / TP lines of an index position (or edit them in the panel,
  BE = stop to entry).
- **🙈 Blind** (replay bar): a random day with all dates hidden (axis, legend, news) — press **Reveal** at the end.
- **Alerts**: 🔔 in a line's settings box (horizontal / trend / ray) or 🔔 on an indicator in the legend → toast, beep,
  desktop notification; in replay it pauses playback so you can decide.
- **⚗ Backtest** (indicator legend): runs the indicator's ▲/▼ signals over a date range with intraday rules, stop / target
  in ATR or points and costs; shows win rate, points, profit factor, drawdown, t-stat, yearly results and every trade.

## Sideways meter (indicator)

ƒx → **Sideways meter**: from 09:45, every bar, the chance that today stays **quiet** (range < 0.8 × the average range
of the last 10 days) — orange above your warning level (default 70 %), with a "sideways?" marker the first time it
crosses. The legend (live) also shows the typical extra range still to come. Model: logistic regression trained
walk-forward on NIFTY 50 1-minute data 2015–2026 (`~/indicators/tests/sideways_meter.py`); out-of-sample 2022–26 AUC 0.75 at
09:45, 0.81 at 10:15, 0.86 at 11:15, and calibrated (60–80 % → 80 % of those days were quiet). It tells you how BIG the
day is likely to be, not its direction, and it did not reliably improve ORB results as a filter — use it to set
expectations (targets, holding time).

## Morning Edge (ML strategy, forward test)
fx Indicators → **Morning Edge (ML, forward test)** on an intraday NIFTY 50 chart (1–15 min). At **09:45** a
gradient-boosting model (`backend/app/edge.py`) predicts NIFTY's 09:45 → 15:15 move (in units of the 10-day average
range) from 52 inputs: the 14 classic strategy signals tested on 2026-10-13 (Gann, opening drive, Dalton, Crabel,
Camarilla, VIX range, momentum, Supertrend, TWAP, RSI, gap, ORB, prev-day H/L, prev-day colour), price context,
breadth of ~115 large caps (Angel quotes at 09:45), yesterday's NIFTY option OI (PCR, walls, OI bias), dealer gamma,
FII / Pro / Client index-futures positions (NSE end-of-day files, downloaded automatically) and VIX.
**|prediction| ≥ trigger → ▲ BUY / ▼ SELL** (ITM option; trigger 0.10 by default, 0.05–0.20 selectable — lower = more trades, less per trade: 0.10 ≈ 9 trades/month, +1.2 option pts, worst DD ₹34k; 0.15 ≈ 6.5/month, +2.2 pts, ₹25k): stop 35 index pts, half booked at +35, the rest's stop moves to
entry, 15:15 square-off. History on the chart is walk-forward (each year from a model trained only on earlier years).
Real ITM-200 premiums 2022 → Jun 2026: 343 trades, win 51 %, +2.2 option pts / trade, PF 1.15 — small; the legend's
**live since 14 Oct 2026** tally is the real test. ChartLab must be running at 09:45 (the decision is made 09:45–09:52
only; a later start skips the day). `GET /api/edge` shows today's decision and its inputs; retrain with
`docker exec chartlab python -m app.edge train`. Data in `data/edge/`.

## Daily Plan (Gann drive + Morning Edge, forward test)
fx Indicators → **Daily Plan: Gann drive + Morning Edge** (5-minute NIFTY chart). At most **one trade a day**: a Gann
opening-drive signal whose entry is before 09:45 → else the 09:45 Morning Edge signal (trigger 0.10 or 0.15) → else a
later Gann signal (until 14:30). Same exits as the two parts (35 stop; Gann 35 target; Morning Edge ½ at +35, rest to
breakeven; 15:15 out). Backtest after costs, 2022–Oct 2026: trigger 0.10 ≈ 10.5 trades/month (~2.5/week), win 56 %,
+2.5 NIFTY pts/trade, PF 1.16, worst DD 447 pts (2023 slightly negative); trigger 0.15 ≈ 7.5/month, win 59 %, +4.6.
The combination was chosen after seeing both parts — the **live since 14 Oct 2026** tally is the real test.

## Gann opening drive (indicator, forward test)
fx Indicators → **Gann opening drive (forward test)** (5-minute NIFTY chart). Draws Gann Square-of-9 levels from the
09:15 open (G0 = open, G±1..±3 at (√open ± 0.25k)², ≈0.33 % apart). Trades only on **strong-opening-drive** days (the
09:15 candle's body ≥ 0.25 × the 10-day average range): the first 5m close beyond G±1 (09:20–14:30) → ▲/▼, entry at the
next open, target 35 / stop 35 (1-minute precision), 15:15 exit; ✓/✗ marks the result. Weak-drive signals are grey
"skip". The legend keeps a **live tally since 13 Oct 2026** — the honest test. Backtest 2022–Jun 2026: win 59 %,
+2.2 pts/trade after costs, ~4 trades a month — found among ~10 clues, so it may be luck (`indicators/tests/gann_trend.py`).
The bell button alerts on each new ▲/▼.

**MCX crude:** the same indicator works on the MCX CRUDEOIL 5-minute chart with crude's session — the first bar of the
day (09:00) is the "09:15 bar", signals until 22:00, square-off 23:00, target = stop = 0.15 × ADR10 (5-minute bars, stop
first). Backtest after 0.03 % costs: ≤2021 win 58 %, +0.08 R; 2022–Oct 2026 win 58 %, +0.10 R, ~2–3 trades a month
(79 trades). Gold, 10 large stocks and BTC lost with the same rule (`indicators/tests/gann_markets.py`), so crude may be
luck as well; its live tally starts 14 Oct 2026. Other non-NSE symbols show a note instead.

## Fake breakout checker (indicator)
fx Indicators → **Fake breakout checker** (5-minute NIFTY chart). At the close of the first 5m bar that closes beyond
PDH / PDL / the 15-minute opening range (09:30–14:30) it shows the chance that price closes back inside the level within
30 minutes (red ≥ 60 %, amber 40–60 %, green < 40 %). 30 minutes later it marks ✓ held / ✗ fake. Logistic model fitted on
NIFTY 2015–21 (`indicators/tests/fake_breakout.py`); out of sample AUC 0.68 (2022–25) / 0.67 (2026), calibrated. The
biggest clue is how far the bar closes past the level. **It predicts fakeouts, not profit**: buying the "green" breakouts
or fading the "red" ones did not make money after costs in 2022–26. Replay-safe (each call appears at its bar's close).

## Pattern coach

- **🤖 Coach** (top bar, or the ⋀⋁ menu): ChartLab finds harmonic patterns itself — confirmed swings only, so in replay
  it only uses candles that have already closed — and draws the newest one leg by leg (X → A → B → C → D). The card
  explains every Fibonacci check in plain words (e.g. "B retraced 44.1% of XA — a Bat needs 0.382–0.500 ✓"), the trade
  idea, and what happened after D. Forming patterns show their projected D zone. Click any pattern in the list to see it.
- **🏆 Pattern challenge** (⋀⋁ menu): ChartLab picks a moment in history where X-A-B-C has just formed. You click
  where you think D will complete and say whether it will reverse; ChartLab locks in its own answer (its reversal zone).
  The replay fast-forwards until the real D is in (price bounced 50% of the C→D leg); the closer answer wins.
  Scoreboard kept in your browser. Honest note: ChartLab's zone contained the real D in only ~13% of historical rounds
  (median miss ≈ 60 points on NIFTY 15m) — harmonic projections are rough, so you can beat it.

## 🎓 Harmonic practice (top bar → 🎓 Harmonics)
Tools that grade **your** work (the coach only shows its own):

| Tool | What it does |
|---|---|
| ✔ **Check my drawing** | Draw an XABCD / Cypher / ABCD, then press **✔ Check** in the drawing's box. Every point is matched to the real swing high/low (orange dots): "B 2 bars late, 6 pts too high". Says which pattern your points fit and which the real swings fit; pick what you thought it was to get a verdict. |
| 📝 **Trade-plan drill** | Replay stops when X‑A‑B‑C has formed. Click entry, stop-loss and target (💡 shows the D zones), lock in, and replay plays on and scores the plan in R. Intraday rules: limit fill only when price trades there, no fills from 15:15, open trade closed at 15:15, order cancelled if price breaks beyond C first. |
| 📈 **My practice stats** | Accuracy per exercise and per pattern, per point (exact / early / late / no swing), confusions ("Bat → you said Gartley"), plan R, last 14 days. Saved in `data/harmonic_practice.json`. |
| 🎯 **D-zone preview** | While you place an XABCD, once X‑A‑B‑C are down the chart shades where D must land for every pattern whose B and C fit (on/off in the menu). |
| 📐 **Ratio cheat sheet** | Live table next to the chart while an XABCD is drawn/selected: legs that fit turn green, broken rules red, fitting patterns highlighted (hidden during flashcards until you answer). |
| 🃏 **Name that pattern** | Flashcards of real patterns from history (indicators hidden), 30 s timer, streak; the answer explains every ratio and what happened after D. |
| 📚 **Pattern library** | Every real completed pattern in a date range (≤ 3 years), filter by type / outcome / direction. ⏪ at C replays it from point C (name and outcome stay hidden until D is confirmed); 👁 shows it whole. Failed ones (incl. "broke X") are the most instructive. |
| 📅 **Daily drill** | 10 rounds a day (4 name, 3 mark, 3 plan), same set all day, with a day streak. "Mark" rounds show a real pattern near the right edge — draw it and it is graded against the real points; drill drawings are removed afterwards. |

All maths runs in the browser (`js/harmonics.js`, mirror of the backend rules, unit-tested); the library uses
`/api/harmonics/library`. In replay only revealed bars are used. "Worked" (≥ 38.2% retrace of AD within 30 bars) is a
low bar — a pattern that "worked" can still lose money as a trade; use the plan drill to see real R.

## News

The **📰 News** tab shows market headlines: live from Economic Times, Business Standard and Mint (every 2 minutes), and
for past days from the GDELT news archive (downloaded when you replay a day, or with **⬇ History**). In replay you only
see headlines published before the replay clock. "on chart" adds N markers to the candles. **📈 Impact**: type a keyword
(RBI, Fed, inflation, crude…) to see how NIFTY moved 15 / 30 / 60 minutes after matching headlines, compared with a
normal move. GDELT allows about one request every 5 s and its times are when an article was seen (≈ 15-minute steps).

## Your own indicators

Put a `.py` file in `custom_indicators/` → it shows up under *Custom* in the Indicators menu (no restart).
See `custom_indicators/README.md` and the example `custom_indicators/example_inside_bar.py`.

## Tests

- `backend/tests` (pytest, synthetic data, no network): resampling, data merging, every API endpoint, the live
  WebSocket feed, custom-indicator hot-reload and error isolation, indicator maths, and the **replay contract for every
  built-in indicator** (computing on data cut at a bar shows exactly what the full run shows up to that bar).
  If the Kaggle data is present, the harmonic engine is also checked on real NIFTY data.
- `frontend/tests` (`node --test`): replay cursor, timeframe switching without leaks, practice-trade engine
  (stops, targets, gaps, averaging, reversal, 15:15 square-off, no overnight carry), Fibonacci / XABCD / R:R maths,
  drawing hit-testing.

## Layout

```
backend/app/        FastAPI server: store.py (data), sources.py (Angel/Yahoo), sync.py, main.py (API + live feed)
backend/app/indicators/   base.py (indicator API), builtin/, lib/harmonic_ref.py, registry.py (custom loader)
frontend/           index.html, css/, js/ (core.js = pure logic, chart, drawings, indicators, replay, practice, app)
custom_indicators/  your indicators
data/               caches, drawings, journal (created at runtime)
```

Charts by TradingView Lightweight Charts™ (Apache-2.0, `frontend/vendor/`).
