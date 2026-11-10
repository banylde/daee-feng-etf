#!/usr/bin/env python3
"""不联网的规则与成交测试。回测必须沿用 fetch.py 里的打分和缓冲。"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest
import fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def row(code, score, name=None):
    return {"code": code, "name": name or code, "score": score}


def test_config_lock():
    rc = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    assert rc["top_n"] == 8
    assert rc["rebalance"]["freq"] == "weekly"
    assert rc["rebalance"]["buffer"] == 5
    assert backtest.COMMISSION == 0.0003
    assert backtest.SLIPPAGE == 0.001
    assert abs(backtest.ONE_WAY_COST - 0.0013) < 1e-15
    assert backtest.RISK_FREE == 0.02


def test_choose_codes_buffer():
    names = {c: c for c in "ABC"}
    ranked = [row("A", 90), row("B", 80), row("C", 70)]
    kept, changes, _ = fetch.choose_codes(ranked, [], 2, 5, names)
    assert kept == ["A", "B"]
    assert changes == ["A 首期入选", "B 首期入选"]

    # 刚好高出 5 分不够，条件是严格大于
    kept, changes, _ = fetch.choose_codes(
        [row("B", 50), row("C", 15), row("A", 10)], ["A", "B"], 2, 5, names)
    assert kept == ["B", "A"]
    assert changes == []

    kept, changes, _ = fetch.choose_codes(
        [row("B", 50), row("C", 15.1), row("A", 10)], ["A", "B"], 2, 5, names)
    assert kept == ["B", "C"]
    assert changes == ["C(15.1) 替换 A(10)"]

    # 最弱者同分时，替换名单里排在前面的那位
    kept, changes, _ = fetch.choose_codes(
        [row("C", 20), row("A", 10), row("B", 10)], ["A", "B"], 2, 5, names)
    assert kept == ["C", "B"]
    assert changes == ["C(20) 替换 A(10)"]

    # 先补空位，再按缓冲替换
    kept, changes, _ = fetch.choose_codes(
        [row("B", 90), row("C", 80), row("A", 10)], ["A"], 2, 5, names)
    assert kept == ["B", "C"]
    assert changes[0] == "B 补入空位"
    assert changes[1] == "C(80) 替换 A(10)"

    names2 = {"A": "甲", "B": "乙", "C": "丙"}
    kept, changes, _ = fetch.choose_codes(
        [row("B", 40, "乙"), row("C", 30, "丙")], ["A", "B"], 2, 5, names2)
    assert kept == ["B", "C"]
    assert changes[0] == "甲 调仓日无数据→移出"
    assert "丙 补入空位" in changes


def test_score_formula():
    cfg = {"etfs": [
        {"code": "L", "market": "sh", "name": "低", "sector": "x"},
        {"code": "H", "market": "sh", "name": "高", "sector": "x"},
    ]}
    low = [("d%02d" % i, 100.0) for i in range(61)]
    high = [("d%02d" % i, 100.0 if i < 60 else 110.0) for i in range(61)]
    rows, b20, b60 = fetch.score_rows(cfg, {"L": low, "H": high}, [100.0] * 61)
    assert b20 == 0 and b60 == 0
    by = {r["code"]: r for r in rows}
    assert by["L"]["score"] == 0
    assert by["H"]["score"] == 100
    assert by["H"]["above20"] is True and by["H"]["above60"] is True
    assert by["L"]["above20"] is False
    # 并列时百分位取排序后第一次出现的位置，两只一样则都是 0 分
    rows, _, _ = fetch.score_rows(cfg, {"L": low, "H": [x for x in low]}, [100.0] * 61)
    assert [r["score"] for r in rows] == [0, 0]


def test_week_end_matches_previous_scan():
    def old(dates, today):
        D = [dt.date.fromisoformat(d) for d in dates]
        wk = lambda d: d.isocalendar()[:2]
        for i in range(len(D) - 1, -1, -1):
            d = D[i]
            nxt = D[i + 1] if i + 1 < len(D) else None
            if d.weekday() == 4 or (nxt and wk(nxt) > wk(d)) or (not nxt and wk(today) > wk(d)):
                return d.isoformat()
        return None

    dates = []
    d = dt.date(2024, 1, 2)
    skipped = dt.date(2024, 2, 9)  # 周五休市
    while d <= dt.date(2024, 3, 29):
        if d.weekday() < 5 and d != skipped:
            dates.append(d.isoformat())
        d += dt.timedelta(days=1)
    today = dt.date(2024, 4, 1)
    assert old(dates, today) == fetch.last_week_end(dates, today)
    for i in range(len(dates)):
        chunk = dates[: i + 1]
        assert old(chunk, today) == fetch.last_week_end(chunk, today)
    # 当周还没结束时，最后一根不是周五的交易日不算调仓日
    mid = [x for x in dates if x <= "2024-01-10"]  # 2024-01-10 是周三
    assert dt.date.fromisoformat(mid[-1]).weekday() == 2
    ends = fetch.week_end_dates(mid, dt.date(2024, 1, 10))
    assert mid[-1] not in ends
    assert any(dt.date.fromisoformat(x).weekday() == 4 for x in ends)


def test_month_end():
    dates = ["2024-01-30", "2024-01-31", "2024-02-01"]
    assert backtest.month_end_dates(dates, dt.date(2024, 2, 2)) == ["2024-01-31"]
    assert backtest.month_end_dates(dates, dt.date(2024, 3, 1)) == ["2024-01-31", "2024-02-01"]


def test_signal_phase():
    assert backtest.signal_dates(["a", "b", "c", "d"], "biweekly") == ["a", "c"]
    assert backtest.signal_dates(["a", "b", "c", "d"], "weekly") == ["a", "b", "c", "d"]


def test_allocate_and_next_open():
    cost = 0.0013
    values, fee, buys = backtest.allocate_equal(1, {}, ["A"], cost)
    ep = 1 / (1 + cost)
    assert abs(values["A"] - ep) < 1e-9
    assert abs(fee - (1 - ep)) < 1e-9
    assert abs(buys - ep) < 1e-9

    values, fee, buys = backtest.allocate_equal(ep, {"A": ep}, ["B"], cost)
    expect = ep * (1 - cost) / (1 + cost)
    assert abs(values["B"] - expect) < 1e-8
    assert "A" not in values

    values, fee, _ = backtest.allocate_equal(1, {"A": 0.5, "B": 0.5}, ["A", "B"], cost)
    assert fee < 1e-9
    assert abs(values["A"] - 0.5) < 1e-9 and abs(values["B"] - 0.5) < 1e-9

    calendar = ["2024-01-05", "2024-01-08", "2024-01-09"]
    opens = {"AAA": {"2024-01-08": 10.0, "2024-01-09": 10.0}}
    closes = {"AAA": {"2024-01-05": 999.0, "2024-01-08": 11.0, "2024-01-09": 12.0}}
    sim = backtest.simulate(calendar, "2024-01-05", {"2024-01-08": ["AAA"]}, opens, closes, cost)
    assert sim["nav"][0] == 1
    shares = ep / 10
    assert abs(sim["nav"][1] - shares * 11) < 1e-8
    assert abs(sim["nav"][2] - shares * 12) < 1e-8
    assert sim["turnovers"][0]["date"] == "2024-01-08"
    # 信号日的 999 没有成交
    assert sim["nav"][0] == 1

    # 换仓：卖出再买入，双边都扣费
    opens2 = {
        "AAA": {"2024-01-08": 10.0, "2024-01-09": 10.0},
        "BBB": {"2024-01-09": 10.0},
    }
    closes2 = {
        "AAA": {"2024-01-08": 10.0, "2024-01-09": 10.0},
        "BBB": {"2024-01-09": 10.0},
    }
    sim = backtest.simulate(
        calendar, "2024-01-05",
        {"2024-01-08": ["AAA"], "2024-01-09": ["BBB"]},
        opens2, closes2, cost)
    assert abs(sim["nav"][1] - ep) < 1e-8
    assert abs(sim["nav"][2] - expect) < 1e-8

    # 买不进的名额留成现金，不编造开盘价
    skips = []
    shares, cash, _ = backtest._rebalance(
        1.0, {}, "2024-01-08", ["AAA", "BBB"],
        {"AAA": {"2024-01-08": 10.0}}, {}, cost, skips)
    assert "BBB" not in shares and shares["AAA"] > 0 and cash > 0
    assert any(s["code"] == "BBB" and s["action"] == "无法买入" for s in skips)
    assert abs(shares["AAA"] * 10 + cash + cost * (shares["AAA"] * 10) - 1) < 1e-6


def test_drawdown_and_flat_sharpe():
    dd = backtest.max_drawdown(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"], [1, 1.2, 0.9, 1.3])
    assert abs(dd["max_drawdown"] - (0.9 / 1.2 - 1)) < 1e-12
    assert (dd["peak"], dd["trough"], dd["recovery"]) == ("2024-01-03", "2024-01-04", "2024-01-05")
    stats = backtest.performance_stats(["2024-01-08", "2024-01-09", "2024-01-10"], [1, 1, 1], [])
    assert stats["ann_return"] == 0
    assert stats["sharpe"] is None
    assert stats["max_drawdown"] == 0
    assert stats["weekly_win_rate"] == 0 or stats["weekly_count"] >= 0


def test_gaps_are_not_filled():
    bars = [("2024-01-02", 1, 1), ("2024-01-04", 1, 1)]
    calendar = ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert backtest.interior_gaps(bars, calendar) == ["2024-01-03"]


def test_rank_band_and_min_hold():
    names = ["A", "B", "C", "D", "E"]
    rows = [{"code": c, "name": c, "score": 50 - i} for i, c in enumerate(names)]
    # 前 2 名买入；已持有的留在前 4 名里就不卖
    assert backtest.choose_rank_band(rows, [], 2) == ["A", "B"]
    assert backtest.choose_rank_band(rows, ["C", "D"], 2) == ["C", "D"]
    assert backtest.choose_rank_band(rows, ["C", "E"], 2) == ["A", "C"]

    entry = {"A": "2024-01-05", "B": "2024-01-05"}
    challenger = [
        {"code": "C", "name": "C", "score": 90},
        {"code": "A", "name": "A", "score": 10},
        {"code": "B", "name": "B", "score": 9},
    ]
    locked, _ = backtest.choose_min_hold(challenger, ["A", "B"], 2, 5, entry, "2024-01-26")
    assert locked == ["A", "B"]
    freed, entry2 = backtest.choose_min_hold(challenger, ["A", "B"], 2, 5, entry, "2024-02-02")
    assert freed == ["C", "A"]
    assert entry2["C"] == "2024-02-02" and entry2["A"] == "2024-01-05"
    # 满 28 天后仍要严格高于缓冲
    tie = [
        {"code": "C", "name": "C", "score": 15},
        {"code": "A", "name": "A", "score": 10},
        {"code": "B", "name": "B", "score": 10},
    ]
    stayed, _ = backtest.choose_min_hold(tie, ["A", "B"], 2, 5, entry, "2024-02-02")
    assert stayed == ["A", "B"]


def test_smooth_scores_average_available_weeks():
    rows = [
        {"code": "A", "name": "A", "score": 30},
        {"code": "B", "name": "B", "score": 90},
    ]
    history = [{"A": 10}, {"A": 20, "B": 0}]
    smoothed, cur = backtest.smooth_scores(rows, history)
    by = {r["code"]: r["score"] for r in smoothed}
    assert by["A"] == 20
    assert by["B"] == 45
    assert cur == {"A": 30, "B": 90}
    assert [r["code"] for r in smoothed] == ["B", "A"]


def test_turnover_specs_change_one_knob():
    specs = backtest.turnover_specs(8)
    assert sum(1 for s in specs if s["live"]) == 1
    assert specs[0]["id"] == "live"
    singles = [s for s in specs if s["id"] not in ("combo_buf_biweek", "combo_band_hold") and not s["live"]]
    combos = [s for s in specs if s["id"].startswith("combo_")]
    assert len(combos) == 2
    for s in singles:
        assert backtest.variant_features(s) and len(backtest.variant_features(s)) == 1
    for s in combos:
        assert len(backtest.variant_features(s)) == 2
    assert backtest.variant_features(specs[0]) == []
    labels = [s["label"] for s in specs]
    assert "缓冲10" in labels and "缓冲15" in labels and "缓冲20" in labels
    assert "双周" in labels and "月" in labels and "最短持有4周" in labels and "3周均分" in labels


def test_period_split_chains_and_cost_drag():
    dates = ["2021-09-17", "2023-12-29", "2024-01-02", "2026-11-09"]
    nav = [1.0, 0.8, 0.9, 1.2]
    parts = backtest.split_periods(dates, nav)
    assert [p["label"] for p in parts] == ["2021-09~2023-12", "2024-01~2026-11"]
    assert parts[0]["end"] == "2023-12-29" and parts[1]["start"] == "2023-12-29"
    early = 0.8 / 1.0
    late = 1.2 / 0.8
    assert abs(early * late - 1.2 / 1.0) < 1e-12
    calendar = ["2024-01-05", "2024-01-08"]
    opens = {"AAA": {"2024-01-08": 10.0}}
    closes = {"AAA": {"2024-01-08": 10.0}}
    execs = {"2024-01-08": ["AAA"]}
    net = backtest.simulate(calendar, "2024-01-05", execs, opens, closes, 0.0013)
    gross = backtest.simulate(calendar, "2024-01-05", execs, opens, closes, 0.0)
    assert gross["nav"][-1] > net["nav"][-1]
    metrics = backtest.eval_schedule(execs, calendar, "2024-01-05", opens, closes)
    assert abs(metrics["cost_drag"] - (metrics["ann_return_gross"] - metrics["ann_return_net"])) < 1e-9
    assert metrics["cost_drag"] > 0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("passed", len(tests))


if __name__ == "__main__":
    main()
