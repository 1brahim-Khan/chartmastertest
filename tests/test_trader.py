import datetime as dt
import json
import unittest
from weekly_trader import APIError, Store, Trader, symbols_from


class FakeAPI:
    def __init__(self, timestamp='2026-10-05T09:30:01-04:00'):
        self.timestamp = timestamp
        self.orders = {}
        self.posts = []
        self.positions = []
        self.cash = '10000'
        self.sessions = [dict(date='2026-10-05', open='09:30', close='16:00'),
                         dict(date='2026-10-09', open='09:30', close='16:00')]
        self.timeout = False

    def find_order(self, client_id):
        return self.orders.get(client_id)

    def request(self, path, data=None):
        if path == '/clock':
            return dict(timestamp=self.timestamp, is_open=True)
        if path.startswith('/calendar'):
            return self.sessions
        if path == '/account':
            return dict(cash=self.cash, buying_power=self.cash)
        if path == '/positions':
            return self.positions
        if path.startswith('/orders?'):
            return []
        if path.startswith('/assets/'):
            return dict(tradable=True, fractionable=True, status='active', **{'class': 'us_equity'})
        if path == '/orders':
            self.posts.append(data)
            result = dict(status='filled', filled_qty=data.get('qty', '10'))
            self.orders[data['client_order_id']] = result
            if self.timeout:
                self.timeout = False
                raise TimeoutError('Response lost after broker accepted order')
            return result
        raise AssertionError(path)


class Tests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = FakeAPI()
        self.trader = Trader(self.api, self.store)
        self.store.execute('INSERT INTO lists VALUES (?,?)', ('2026-10-05', json.dumps(['AAPL', 'MSFT'])))

    def seed(self, symbol, qty):
        self.store.execute('INSERT INTO orders(client_id, week,symbol,side,status,filled_qty) VALUES(?,?,?,?,?,?)',
                           ('seed-' + symbol, '2026-09-28', symbol, 'buy', 'filled', qty))

    def test_buys_exact_notional_once(self):
        self.trader.tick()
        self.trader.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.assertTrue(all(o['notional'] == '5000' and o['time_in_force'] == 'day' for o in self.api.posts))

    def test_timeout_reconciles_without_duplicate(self):
        self.api.timeout = True
        with self.assertRaises(TimeoutError):
            self.trader.tick()
        self.trader.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(self.store.owned()['AAPL'], 10)

    def test_only_owned_losers_sold(self):
        for symbol in ('AAPL', 'MSFT', 'ZERO', 'MISMATCH'):
            self.seed(symbol, '10')
        self.api.timestamp = '2026-10-09T15:55:01-04:00'
        self.api.positions = [dict(symbol=s, qty=q, unrealized_pl=pl) for s, q, pl in
                              [('AAPL', '10', '-1'), ('MSFT', '10', '1'), ('ZERO', '10', '0'),
                               ('MANUAL', '10', '-3'), ('MISMATCH', '11', '-4')]]
        self.trader.tick()
        self.trader.tick()
        self.assertEqual([o['symbol'] for o in self.api.posts], ['AAPL'])
        self.assertEqual(self.api.posts[0]['qty'], '10')

    def test_holiday_and_early_close(self):
        self.api.sessions = [dict(date='2026-10-06', open='09:30', close='16:00'),
                             dict(date='2026-10-08', open='09:30', close='13:00')]
        self.api.timestamp = '2026-10-06T09:30:00-04:00'
        self.trader.tick()
        self.assertEqual(len(self.api.posts), 2)
        self.api.timestamp = '2026-10-08T12:55:00-04:00'
        self.api.positions = [dict(symbol='AAPL', qty='10', unrealized_pl='-10')]
        self.trader.tick()
        self.assertEqual(self.api.posts[-1]['side'], 'sell')

    def test_no_late_buys(self):
        self.api.timestamp = '2026-10-05T09:35:00-04:00'
        self.trader.tick()
        self.assertEqual(self.api.posts, [])

    def test_insufficient_cash(self):
        self.api.cash = '4999'
        self.trader.tick()
        self.assertEqual(self.api.posts, [])

    def test_repeat_skip(self):
        self.seed('AAPL', '10')
        self.api.positions = [dict(symbol='AAPL', qty='10', unrealized_pl='1')]
        Trader(self.api, self.store, repeat='skip').tick()
        self.assertEqual([o['symbol'] for o in self.api.posts], ['MSFT'])

    def test_unmanaged_position_not_bought(self):
        self.api.positions = [dict(symbol='AAPL', qty='10', unrealized_pl='1')]
        self.trader.tick()
        self.assertEqual([o['symbol'] for o in self.api.posts], ['MSFT'])

    def test_normalization(self):
        self.assertEqual(symbols_from('aapl, MSFT aapl'), ['AAPL', 'MSFT'])
        with self.assertRaises(ValueError):
            symbols_from('BTC/USD')

    def test_closed_market_never_submits(self):
        original = self.api.request
        def closed(path, data=None):
            if path == '/clock':
                return dict(timestamp=self.api.timestamp, is_open=False)
            return original(path, data)
        self.api.request = closed
        self.trader.tick()
        self.assertEqual(self.api.posts, [])

    def test_partial_fill_is_reconciled(self):
        self.trader.tick()
        client_id = 'weekly-2026-10-05-buy-AAPL'
        self.store.execute('UPDATE orders SET status=?, filled_qty=? WHERE client_id=?',
                           ('partially_filled', '2', client_id))
        self.api.orders[client_id] = dict(status='filled', filled_qty='10')
        self.trader.tick()
        self.assertEqual(self.store.owned()['AAPL'], 10)
        self.assertEqual(len(self.api.posts), 2)

    def test_rejected_order_does_not_retry(self):
        original = self.api.request
        def reject(path, data=None):
            if path == '/orders':
                raise APIError(403, 'insufficient buying power')
            return original(path, data)
        self.api.request = reject
        with self.assertRaises(APIError):
            self.trader.tick()
        self.api.request = original
        self.trader.tick()
        self.assertEqual([o['symbol'] for o in self.api.posts], ['MSFT'])


if __name__ == '__main__':
    unittest.main()
