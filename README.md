# Weekly Alpaca trader

A small Python 3.11+ command-line tool for your weekly watchlist. No packages to install. Runs on macOS/Linux.

## Strategy

- Save a fresh watchlist each Sunday. Lists never carry forward automatically.
- On Monday, submit a **$5,000 market buy per ticker** just after the regular market opens. The scheduler polls every two seconds; broker latency and fills mean execution is not guaranteed at the opening price.
- Repeated tickers add another $5,000 by default. Use `--repeat skip` to keep existing holdings without adding.
- Five minutes before Friday close, sell the entire managed position if Alpaca reports negative unrealized P/L relative to its average purchase cost. Keep positive and flat positions, including older holdings.
- On holidays, buy at the week's first trading session and evaluate losses before its last trading session closes. Alpaca's calendar handles early closes.

**Friday timing:** you cannot know the final closing price and also sell based on that price before the market closes. This implementation checks five minutes before close (normally 3:55 p.m. New York time). Set `--close-minutes 1` for a later check. It does not implement final-close evaluation with next-session selling. Once a sell is submitted, a later price recovery does not cancel it.

## Setup

Create paper API keys in your [Alpaca dashboard](https://app.alpaca.markets/). Set them in the shell that runs the tool; keep the secret out of watchlists and source files:

```sh
export APCA_API_KEY_ID='your-paper-key'
export APCA_API_SECRET_KEY='your-paper-secret'
```

On Sunday, save next week's list:

```sh
python3 weekly_trader.py watchlist AAPL MSFT NVDA
```

You can explicitly choose a future Monday:

```sh
python3 weekly_trader.py watchlist --week 2026-10-05 AAPL MSFT
```

Start the scheduler and leave it running:

```sh
python3 weekly_trader.py run
```

The computer must remain awake and connected at the scheduled times. For unattended use, run on an always-on macOS/Linux host with a process supervisor. On macOS, `caffeinate -i python3 weekly_trader.py run` prevents idle sleep while it runs. Save each Sunday's watchlist from another terminal in the same folder; saving works while the scheduler is running. Press Ctrl-C to stop it.

Inspect lists, orders, and managed shares:

```sh
python3 weekly_trader.py status
```

`once` executes one tick with the same time restrictions. `run --repeat skip --close-minutes 1` changes the two strategy settings; keep those flags consistent across restarts. Commands must use the same working directory or an explicit absolute `--db` path.

## Live trading

Default mode is paper trading. To trade real money, set **live** account keys and pass `--live` on every command:

```sh
python3 weekly_trader.py --live watchlist AAPL MSFT
python3 weekly_trader.py --live run
python3 weekly_trader.py --live status
```

Live and paper modes have separate SQLite ledgers, each bound to its Alpaca account ID. Never delete a ledger containing holdings, share it between accounts, or run multiple different ledgers against the same account. Back it up while the scheduler is stopped.

## Order handling

Only active fractional US equities are accepted because exact dollar sizing requires fractional orders. Market orders use `day` time in force. Each watchlist of N tickers requires $5,000 × N available cash; the tool avoids borrowing against margin buying power. Insufficient funds or invalid assets are logged and skipped; earlier tickers in alphabetical order may still buy.

Orders have deterministic client IDs and a persisted intent written before submission. A lost API response is reconciled with Alpaca before another attempt. Pending fills are polled and recorded. Rejected, canceled, expired, or partially filled then canceled orders are not automatically replaced or topped up; inspect `status` and broker records. A timeout is retried only during the trade window. The tool does not buy after the first five minutes of the opening session and does not make up missed Friday sales after close.

Only shares purchased through this ledger can be sold. Existing unrelated tickers are left alone. If someone manually trades a managed ticker or a corporate action changes its quantity, a mismatch blocks further trades for that symbol until reviewed. Avoid manual trades in managed symbols; a dedicated strategy account is easiest to maintain. Do not edit ledger quantities without reconciling broker records.

Keep the logs and check order failures. The tool does not send notifications, guarantee fills before close, or run from this chat. No credentials were configured and no broker orders were submitted during development.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Tests use a simulated API, including timeouts after broker acceptance, repeat ticks, holidays, early closes, cash limits, and managed-position ownership. Real Alpaca connectivity and order fills require your credentials and a paper trading run.

API references: [orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [fractional trading](https://docs.alpaca.markets/us/docs/fractional-trading), [calendar SDK](https://alpaca.markets/sdks/python/api_reference/trading/calendar.html), [clock SDK](https://alpaca.markets/sdks/python/api_reference/trading/clock.html).
