import pandas as pd

from app import flows


def test_parse_nse_cash_row():
    js = [{"buyValue": "16,242.83", "category": "DII", "date": "22-Oct-2026", "netValue": "2891.32", "sellValue": "13351.51"},
          {"buyValue": "13627.54", "category": "FII/FPI", "date": "22-Oct-2026", "netValue": "-3458.7", "sellValue": "17086.24"}]
    r = flows.parse_nse(js)
    assert r["date"] == pd.Timestamp("2026-10-22") and r["fii_net"] == -3458.7 and r["dii_buy"] == 16242.83
    assert flows.parse_nse([]) is None


def test_parse_nsdl_rows_and_negative_brackets():
    html = ("<tr><td>21-Oct-2026</td><td>Equity</td><td>Stock Exchange</td><td>12,000.50</td><td>13,500.00</td>"
            "<td>(1,499.50)</td></tr><tr><td>Debt</td><td>Stock Exchange</td><td>10.00</td><td>5.00</td><td>5.00</td></tr>")
    rows = flows.parse_nsdl(html)
    assert len(rows) == 1 and rows[0][0].isoformat() == "2026-10-21" and rows[0][3] == -1499.5 and rows[0][4] == 5.0


def test_positions_long_pct_and_day_change():
    pa = pd.DataFrame({"date": pd.to_datetime(["2026-10-21", "2026-10-22"]), "Client Type": ["FII", "FII"],
                       "Future Index Long": [30000, 34703], "Future Index Short": [320000, 315003],
                       "Future Stock Long": [1, 1], "Future Stock Short": [0, 0],
                       "Option Index Call Long": [10, 20], "Option Index Call Short": [5, 5],
                       "Option Index Put Long": [8, 8], "Option Index Put Short": [10, 2]})
    p = flows.positions(pa)
    last = p.iloc[-1]
    assert round(last.idx_fut_long_pct, 1) == 9.9 and last.idx_fut_net == 34703 - 315003
    assert last.idx_fut_net_chg == (34703 - 315003) - (30000 - 320000) and last.idx_put_net == 6
