#!/usr/bin/env python3
"""Persistent weekly Alpaca equities trader. Python 3.11+, no dependencies."""
import argparse
import contextlib
import datetime as dt
from decimal import Decimal
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced', 'failed'}
LOG = logging.getLogger('weekly_trader')


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f'Alpaca HTTP {status}: {message}')


class Alpaca:
    def __init__(self, live=False):
        self.base = 'https://api.alpaca.markets' if live else 'https://paper-api.alpaca.markets'
        self.headers = {'APCA-API-KEY-ID': os.environ['APCA_API_KEY_ID'],
                        'APCA-API-SECRET-KEY': os.environ['APCA_API_SECRET_KEY'],
                        'Content-Type': 'application/json'}

    def request(self, path, data=None):
        req = urllib.request.Request(self.base + '/v2' + path,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise APIError(error.code, error.read().decode()) from error

    def find_order(self, client_id):
        try:
            return self.request('/orders:by_client_order_id?' + urllib.parse.urlencode({'client_order_id': client_id}))
        except APIError as error:
            if error.status == 404:
                return None
            raise


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS lists (week TEXT PRIMARY KEY, symbols TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS orders (
            client_id TEXT PRIMARY KEY, week TEXT, symbol TEXT, side TEXT,
            payload TEXT, status TEXT, filled_qty TEXT DEFAULT '0', detail TEXT);
        ''')

    def rows(self, query, args=()):
        return self.db.execute(query, args).fetchall()

    def execute(self, query, args=()):
        with self.db:
            self.db.execute(query, args)

    def owned(self):
        result = {}
        for row in self.rows('SELECT symbol, side, filled_qty FROM orders'):
            sign = 1 if row['side'] == 'buy' else -1
            result[row['symbol']] = result.get(row['symbol'], Decimal(0)) + sign * Decimal(row['filled_qty'])
        return {s: q for s, q in result.items() if q > 0}


def monday(day):
    return day - dt.timedelta(days=day.weekday())


def symbols_from(text):
    symbols = sorted(set(s.upper() for s in re.split(r'[\s,]+', text.strip()) if s))
    if not symbols or any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', s) for s in symbols):
        raise ValueError('Provide one or more US stock tickers separated by commas or spaces.')
    return symbols


class Trader:
    def __init__(self, api, store, repeat='add', close_minutes=5):
        self.api, self.store = api, store
        self.repeat, self.close_minutes = repeat, close_minutes
        self.calendar_cache = {}

    def update(self, client_id, order):
        self.store.execute('UPDATE orders SET status=?, filled_qty=?, detail=? WHERE client_id=?',
                           (order['status'], order.get('filled_qty', '0'), json.dumps(order), client_id))

    def reconcile(self):
        placeholders = ','.join('?' for _ in TERMINAL)
        for row in self.store.rows(f'SELECT * FROM orders WHERE status NOT IN ({placeholders})', tuple(TERMINAL)):
            order = self.api.find_order(row['client_id'])
            if order:
                self.update(row['client_id'], order)

    def submit(self, week, symbol, side, amount):
        client_id = f'weekly-{week}-{side}-{symbol}'
        existing = self.store.rows('SELECT * FROM orders WHERE client_id=?', (client_id,))
        if existing and existing[0]['status'] != 'intent':
            return
        payload = {'symbol': symbol, 'side': side, 'type': 'market', 'time_in_force': 'day',
                   'extended_hours': False, 'client_order_id': client_id,
                   'notional' if side == 'buy' else 'qty': str(amount)}
        if not existing:
            self.store.execute('INSERT INTO orders(client_id,week,symbol,side,payload,status) VALUES(?,?,?,?,?,?)',
                               (client_id, week, symbol, side, json.dumps(payload), 'intent'))
        else:
            payload = json.loads(existing[0]['payload'])
        # Lookup before every attempt: a timed-out POST may already have succeeded.
        order = self.api.find_order(client_id)
        if order:
            self.update(client_id, order)
            return
        clock = self.api.request('/clock')
        current = dt.datetime.fromisoformat(clock['timestamp']).astimezone(ET)
        window = self.buy_window if side == 'buy' else self.sell_window
        if not clock['is_open'] or not window[0] <= current < window[1]:
            LOG.warning('Order window elapsed for %s %s; intent remains unsubmitted.', side, symbol)
            return
        try:
            order = self.api.request('/orders', payload)
        except APIError as error:
            if error.status == 422:
                found = self.api.find_order(client_id)
                if found:
                    self.update(client_id, found)
                    return
            # Duplicate IDs and server failures remain intents until reconciled.
            if error.status in {400, 401, 403, 422}:
                self.store.execute('UPDATE orders SET status=?, detail=? WHERE client_id=?',
                                   ('failed', str(error), client_id))
            raise
        self.update(client_id, order)
        LOG.info('%s %s: %s (%s)', side.upper(), symbol, amount, order['status'])

    def tick(self):
        clock = self.api.request('/clock')
        now = dt.datetime.fromisoformat(clock['timestamp']).astimezone(ET)
        self.reconcile()
        if not clock['is_open']:
            return
        start = monday(now.date())
        week = start.isoformat()
        if week not in self.calendar_cache:
            self.calendar_cache[week] = self.api.request('/calendar?' + urllib.parse.urlencode(
                {'start': week, 'end': (start + dt.timedelta(days=4)).isoformat()}))
        sessions = self.calendar_cache[week]
        if not sessions:
            return
        def moment(session, field):
            return dt.datetime.fromisoformat(session['date'] + 'T' + session[field]).replace(tzinfo=ET)
        first, last = sessions[0], sessions[-1]
        self.buy_window = (moment(first, 'open'), moment(first, 'open') + dt.timedelta(minutes=5))
        self.sell_window = (moment(last, 'close') - dt.timedelta(minutes=self.close_minutes), moment(last, 'close'))
        buying = self.buy_window[0] <= now < self.buy_window[1]
        selling = self.sell_window[0] <= now < self.sell_window[1]
        if not buying and not selling:
            return
        account = self.api.request('/account')
        if account.get('trading_blocked') or account.get('account_blocked'):
            raise RuntimeError('Account trading is blocked.')
        positions = {p['symbol']: p for p in self.api.request('/positions')}
        open_orders = self.api.request('/orders?status=open&limit=500')
        owned = self.store.owned()
        if selling:
            for symbol, qty in owned.items():
                position = positions.get(symbol)
                if not position or Decimal(position['qty']) != qty:
                    LOG.error('Position mismatch for %s; check manual trades/corporate actions. Sale skipped.', symbol)
                    continue
                if Decimal(position['unrealized_pl']) >= 0:
                    continue
                if any(o['symbol'] == symbol for o in open_orders):
                    continue
                self.submit(week, symbol, 'sell', qty)
        if buying:
            lists = self.store.rows('SELECT symbols FROM lists WHERE week=?', (week,))
            if not lists:
                LOG.warning('No watchlist for %s.', week)
                return
            cash = min(Decimal(account['cash']), Decimal(account['buying_power']))
            for symbol in json.loads(lists[0]['symbols']):
                if positions.get(symbol) and (symbol not in owned or Decimal(positions[symbol]['qty']) != owned[symbol]):
                    LOG.error('Unmanaged/mismatched position for %s; buy skipped.', symbol)
                    continue
                if self.repeat == 'skip' and symbol in owned:
                    continue
                if self.store.rows('SELECT 1 FROM orders WHERE week=? AND symbol=? AND side=? AND status != ?',
                                   (week, symbol, 'buy', 'intent')):
                    continue
                if any(o['symbol'] == symbol for o in open_orders):
                    continue
                asset = self.api.request('/assets/' + urllib.parse.quote(symbol))
                if asset['class'] != 'us_equity' or not asset['tradable'] or not asset.get('fractionable') or asset['status'] != 'active':
                    LOG.error('%s is not an active, tradable fractional US equity; skipped.', symbol)
                    continue
                if cash < 5000:
                    LOG.error('Insufficient available cash for %s; skipped.', symbol)
                    continue
                self.submit(week, symbol, 'buy', Decimal('5000'))
                cash -= 5000


@contextlib.contextmanager
def lock(path):
    with open(str(path) + '.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another process is using this ledger.') from None
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use live account and separate live ledger')
    parser.add_argument('--db', help='Ledger path; do not share between accounts')
    sub = parser.add_subparsers(dest='command', required=True)
    watch = sub.add_parser('watchlist', help='Save the upcoming Monday watchlist')
    watch.add_argument('symbols', nargs='+')
    watch.add_argument('--week', help='Monday date YYYY-MM-DD; defaults to next Monday')
    sub.add_parser('status', help='Show saved lists and order ledger; no API call')
    for command in ('run', 'once'):
        run = sub.add_parser(command, help='Run scheduler' if command == 'run' else 'Process one scheduled tick')
        run.add_argument('--repeat', choices=['add', 'skip'], default='add')
        run.add_argument('--close-minutes', type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    path = Path(args.db or ('live.sqlite3' if args.live else 'paper.sqlite3')).resolve()
    with lock(path) if args.command in {'run', 'once'} else contextlib.nullcontext():
        store = Store(path)
        if args.command == 'watchlist':
            today = dt.datetime.now(ET).date()
            week = dt.date.fromisoformat(args.week) if args.week else monday(today) + dt.timedelta(days=7)
            if week.weekday() != 0 or week <= today:
                raise ValueError('Watchlist week must be a future Monday.')
            symbols = symbols_from(' '.join(args.symbols))
            store.execute('INSERT OR REPLACE INTO lists VALUES (?,?)', (week.isoformat(), json.dumps(symbols)))
            print(f'Saved {week}: {", ".join(symbols)} (${5000 * len(symbols):,} total)')
        elif args.command == 'status':
            print(json.dumps({'watchlists': [dict(r) for r in store.rows('SELECT * FROM lists ORDER BY week')],
                              'orders': [dict(r) for r in store.rows('SELECT client_id,symbol,side,status,filled_qty,detail FROM orders')],
                              'owned': {s: str(q) for s, q in store.owned().items()}}, indent=2))
        else:
            if not 1 <= args.close_minutes <= 30:
                raise ValueError('--close-minutes must be between 1 and 30.')
            api = Alpaca(args.live)
            # Bind a ledger to an account so credentials cannot silently switch its ownership.
            account_id = api.request('/account')['id']
            store.db.execute('CREATE TABLE IF NOT EXISTS identity (id TEXT PRIMARY KEY)')
            identity = store.rows('SELECT id FROM identity')
            if identity and identity[0]['id'] != account_id:
                raise RuntimeError('This ledger belongs to a different Alpaca account.')
            store.execute('INSERT OR IGNORE INTO identity VALUES (?)', (account_id,))
            trader = Trader(api, store, args.repeat, args.close_minutes)
            while True:
                try:
                    trader.tick()
                except Exception:
                    LOG.exception('Tick failed; consult ledger and logs before intervening.')
                    if args.command == 'once':
                        raise
                if args.command == 'once':
                    break
                time.sleep(2)


if __name__ == '__main__':
    main()
