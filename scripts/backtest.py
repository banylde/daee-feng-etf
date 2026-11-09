#!/usr/bin/env python3
"""八友轮动历史回测。

选股、打分、周末信号和缓冲替换直接调用 scripts/fetch.py，不另写一套规则。
信号使用当日收盘价；成交使用下一交易日开盘价。没有开盘价或收盘价的日子不补、不插值。
"""
import json, os, sys, time, datetime as dt
import urllib.request

import fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BJ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

# 回测费用。看板的实盘规则不含这些数字，也不在这里改调仓阈值。
COMMISSION = 0.0003  # 单边万分之三
SLIPPAGE = 0.001     # 千分之一
ONE_WAY_COST = COMMISSION + SLIPPAGE
RISK_FREE = 0.02
ANN_DAYS = 252
WARMUP_DAYS = 140  # 5 年收益窗口之前，再留出打分所需的 60 个交易日

HS300_ETF = {"code": "510300", "market": "sh", "name": "沪深300ETF"}

SAMPLE_NOTE = (
    "主回测与缓冲、频率对照使用同一段历史行情，同属样本内计算。"
    "对照只展示这两项设定变化时指标如何变动。实盘规则保持每周调仓、5分缓冲。"
)
BIWEEKLY_NOTE = (
    "双周从样本内第一个有效周末信号起，取第 1、3、5… 次信号调仓，中间那个周末不调仓。"
    "相位跟这段样本的起点绑定。"
)
DISCLAIMER = "回测不代表未来收益"
CACHE_PATH = "/tmp/feng-etf-backtest-cache.json"


def request_start(today):
    try:
        anchor = today.replace(year=today.year - 5)
    except ValueError:
        anchor = today.replace(year=today.year - 5, day=28)
    return (anchor - dt.timedelta(days=WARMUP_DAYS)).isoformat()


def r6(x):
    if x is None:
        return None
    return round(float(x), 6)


def http_json(url, headers=None, tries=4):
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


_LAST_HTTP = [0.0]


def paced_json(url, headers=None):
    wait = 0.2 - (time.time() - _LAST_HTTP[0])
    if wait > 0:
        time.sleep(wait)
    try:
        return http_json(url, headers)
    finally:
        _LAST_HTTP[0] = time.time()


def clean_bars(rows):
    """丢掉非正价格。同一天两套不同的价格视为冲突，不取平均。"""
    out = []
    seen = {}
    bad = 0
    for d, o, c in rows:
        if o is None or c is None or o <= 0 or c <= 0 or o != o or c != c:
            bad += 1
            continue
        prev = seen.get(d)
        if prev is not None and (abs(prev[0] - o) > 1e-6 or abs(prev[1] - c) > 1e-6):
            raise RuntimeError(f"{d} 出现两套不同价格 {prev} / {(o, c)}")
        seen[d] = (o, c)
    for d in sorted(seen):
        out.append((d, seen[d][0], seen[d][1]))
    return out, bad


def tencent_bars(code, market, start, end):
    sym = market + code
    bars = {}
    field = None
    end_cursor = end
    for _ in range(12):
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,600,qfq" % (sym, start, end_cursor)
        data = paced_json(url)
        node = (data.get("data") or {}).get(sym) or {}
        if node.get("qfqday"):
            page_field, k = "qfqday", node["qfqday"]
        elif node.get("day"):
            page_field, k = "day", node["day"]
        else:
            page_field, k = None, []
        if page_field and field and page_field != field:
            raise RuntimeError("%s 分页复权字段不一致" % sym)
        field = page_field or field
        if not k:
            break
        for row in k:
            d, o, c = row[0], float(row[1]), float(row[2])
            if start <= d <= end:
                prev = bars.get(d)
                if prev is not None and (abs(prev[0] - o) > 1e-6 or abs(prev[1] - c) > 1e-6):
                    raise RuntimeError("%s %s 分页价格冲突" % (sym, d))
                bars[d] = (o, c)
        first = k[0][0]
        if first <= start or len(k) < 500:
            break
        prev_day = (dt.date.fromisoformat(first) - dt.timedelta(days=1)).isoformat()
        if prev_day >= end_cursor:
            break
        end_cursor = prev_day
    ordered = [(d, bars[d][0], bars[d][1]) for d in sorted(bars)]
    return ordered, field


def eastmoney_bars(code, market, start, end):
    secid = ("1." if market == "sh" else "0.") + code
    beg = start.replace("-", "")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3"
           "&fields2=f51,f52,f53&klt=101&fqt=1&beg=%s&end=20500101&lmt=100000"
           "&ut=fa5fd1943c7b386f172d6893dbfba10b") % (secid, beg)
    data = paced_json(url)
    node = data.get("data") or {}
    rows = []
    for line in node.get("klines") or []:
        p = line.split(",")
        d = p[0]
        if start <= d <= end:
            rows.append((d, float(p[1]), float(p[2])))
    return rows, node.get("name")


def sina_bars(code, market, start, end):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
           "?symbol=%s%s&scale=240&ma=no&datalen=2000") % (market, code)
    data = paced_json(url, {"Referer": "https://finance.sina.com.cn", "User-Agent": UA["User-Agent"]})
    rows = []
    for x in data or []:
        d = x["day"]
        if start <= d <= end:
            rows.append((d, float(x["open"]), float(x["close"])))
    return rows


def prices_agree(a, b):
    mb = {d: (o, c) for d, o, c in b}
    both = mism = 0
    for d, o, c in a:
        if d not in mb:
            continue
        both += 1
        bo, bc = mb[d]
        if abs(o - bo) > 0.001 or abs(c - bc) > 0.001:
            mism += 1
    return both, mism


def load_symbol(code, market, start, end):
    """腾讯前复权优先。没有 qfqday 时与东方财富前复权对照，不一致就整段改用东方财富。"""
    errors = []
    tbars, field = [], None
    try:
        tbars, field = tencent_bars(code, market, start, end)
        tbars, bad = clean_bars(tbars)
    except Exception as e:
        errors.append("tencent: %s" % e)
        tbars, field, bad = [], None, 0
    if len(tbars) >= 1 and field == "qfqday":
        return tbars, {"source": "tencent", "adjust": "qfq", "bad_ticks": bad,
                       "note": "腾讯前复权"}
    if len(tbars) >= 1 and field == "day":
        try:
            ebars, _ = eastmoney_bars(code, market, start, end)
            ebars, ebad = clean_bars(ebars)
            both, mism = prices_agree(tbars, ebars)
            shorter = min(len(tbars), len(ebars))
            if shorter >= 5 and both >= shorter * 0.9 and mism == 0:
                if ebars and tbars and (ebars[0][0] < tbars[0][0] or len(ebars) > len(tbars) + 3):
                    return ebars, {"source": "eastmoney", "adjust": "qfq", "bad_ticks": ebad,
                                   "note": "腾讯重叠区间与东方财富前复权一致，东方财富覆盖更完整，采用东方财富"}
                return tbars, {"source": "tencent", "adjust": "qfq", "bad_ticks": bad,
                               "note": "腾讯未单列 qfqday，与东方财富前复权一致"}
            if len(ebars) >= 1:
                return ebars, {"source": "eastmoney", "adjust": "qfq", "bad_ticks": ebad,
                               "note": "腾讯未提供前复权，改用东方财富前复权"}
        except Exception as e:
            errors.append("eastmoney: %s" % e)
        return tbars, {"source": "tencent", "adjust": "unverified", "bad_ticks": bad,
                       "note": "腾讯未单列前复权，东方财富对照失败，暂用腾讯日线"}
    try:
        ebars, _ = eastmoney_bars(code, market, start, end)
        ebars, ebad = clean_bars(ebars)
        if ebars:
            return ebars, {"source": "eastmoney", "adjust": "qfq", "bad_ticks": ebad,
                           "note": "腾讯抓取失败，改用东方财富前复权"}
    except Exception as e:
        errors.append("eastmoney: %s" % e)
    try:
        sbars = sina_bars(code, market, start, end)
        sbars, sbad = clean_bars(sbars)
        if sbars:
            return sbars, {"source": "sina", "adjust": "none", "bad_ticks": sbad,
                           "note": "腾讯与东方财富都失败，改用新浪不复权"}
    except Exception as e:
        errors.append("sina: %s" % e)
    return [], {"source": None, "adjust": None, "bad_ticks": 0, "note": "；".join(errors) or "无数据"}


def price_maps(bars):
    opens, closes = {}, {}
    for d, o, c in bars:
        opens[d] = o
        closes[d] = c
    return opens, closes


def month_end_dates(dates, today):
    """月末最后一个交易日：下一交易日进入新月份，或样本已结束且今天已进入新月份。"""
    today = fetch.as_date(today)
    D = [fetch.as_date(d) for d in dates]
    out = []
    for i, d in enumerate(D):
        nxt = D[i + 1] if i + 1 < len(D) else None
        ended = (nxt is not None and (nxt.year, nxt.month) > (d.year, d.month)) or (
            nxt is None and (today.year, today.month) > (d.year, d.month))
        if ended:
            out.append(d.isoformat())
    return out


def solve_weights(equity, current, weights, cost_rate):
    """按权重配置可交易持仓。权重之和可以小于 1，剩下的留在现金。

    成交额按开盘价计价，每一边扣除佣金加滑点。返回 (目标市值, 费用, 买入额)。
    """
    equity = float(equity)
    weights = {c: float(w) for c, w in weights.items() if w > 0}
    current = {c: float(v) for c, v in current.items() if v > 0}
    if equity <= 0:
        return {}, 0.0, 0.0
    wsum = sum(weights.values())
    if wsum > 1 + 1e-9:
        raise RuntimeError("权重大于 1")
    codes = list(dict.fromkeys(list(weights) + list(current)))

    def traded_of(ep):
        traded = 0.0
        for c in codes:
            tgt = weights.get(c, 0.0) * ep
            traded += abs(tgt - current.get(c, 0.0))
        return traded

    lo, hi = 0.0, equity
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if mid + traded_of(mid) * cost_rate > equity:
            hi = mid
        else:
            lo = mid
    ep = (lo + hi) / 2.0
    values = {}
    buys = 0.0
    traded = traded_of(ep)
    for c, w in weights.items():
        tgt = w * ep
        if tgt > 0:
            values[c] = tgt
            buys += max(0.0, tgt - current.get(c, 0.0))
    return values, traded * cost_rate, buys


def allocate_equal(equity, current, targets, cost_rate):
    n = len(targets)
    if n == 0:
        values, cost, buys = solve_weights(equity, current, {}, cost_rate)
        return values, cost, buys
    weights = {c: 1.0 / n for c in targets}
    return solve_weights(equity, current, weights, cost_rate)


def _rebalance(cash, shares, date, targets, opens, last_close, cost_rate, skips):
    equity = cash
    current = {}
    stuck = {}
    stuck_value = 0.0
    for code, sh in shares.items():
        px = opens.get(code, {}).get(date)
        if px is None or px <= 0:
            mark_px = last_close.get(code)
            if mark_px is None:
                raise RuntimeError("%s %s 无开盘价，也没有更早的收盘价" % (date, code))
            stuck[code] = sh
            stuck_value += sh * mark_px
            skips.append({"date": date, "code": code, "action": "无法卖出", "reason": "执行日没有开盘价"})
        else:
            current[code] = sh * px
            equity += current[code]
    equity += stuck_value
    buyable = []
    for code in targets:
        px = opens.get(code, {}).get(date)
        if px is None or px <= 0:
            skips.append({"date": date, "code": code, "action": "无法买入", "reason": "执行日没有开盘价"})
        else:
            buyable.append(code)
    n = len(targets)
    free = equity - stuck_value
    if n == 0:
        values, cost, buys = allocate_equal(free, current, [], cost_rate)
        new_shares = dict(stuck)
        cash = free - cost
        return new_shares, cash, buys / equity if equity else 0.0
    if not stuck and len(buyable) == n:
        values, cost, buys = allocate_equal(equity, current, buyable, cost_rate)
        new_shares = {}
        for code, val in values.items():
            new_shares[code] = val / opens[code][date]
        cash = equity - cost - sum(values.values())
        return new_shares, cash, buys / equity if equity else 0.0
    # 买不进的名额留成现金，目标权重仍是 1/原只数。卖不出的仓位锁住。
    weights = {c: 1.0 / n for c in buyable}
    values, cost, buys = _solve_with_locked(free, stuck_value, current, weights, cost_rate)
    invested = sum(values.values())
    cash_left = free - cost - invested
    if cash_left < -1e-6:
        values, cost, buys = allocate_equal(free, current, buyable, cost_rate)
        cash_left = free - cost - sum(values.values())
        skips.append({"date": date, "code": "", "action": "改为可成交部分等权",
                      "reason": "锁住仓位之后无法再按原只数等权"})
    new_shares = dict(stuck)
    for code, val in values.items():
        new_shares[code] = val / opens[code][date]
    return new_shares, cash_left, buys / equity if equity else 0.0


def _solve_with_locked(free, locked_value, current, weights, cost_rate):
    """weights 是占总权益（含锁仓）的比例。锁仓不参与成交。"""
    codes = list(dict.fromkeys(list(weights) + list(current)))

    def traded_of(total_ep):
        traded = 0.0
        for c in codes:
            tgt = weights.get(c, 0.0) * total_ep
            traded += abs(tgt - current.get(c, 0.0))
        return traded

    # 费用后总权益 E = free + locked - cost，且 E >= locked
    lo, hi = locked_value, free + locked_value
    for _ in range(80):
        mid = (lo + hi) / 2.0
        cost = traded_of(mid) * cost_rate
        if locked_value + (free - cost) > mid:
            lo = mid
        else:
            hi = mid
    ep = (lo + hi) / 2.0
    values = {}
    buys = 0.0
    traded = traded_of(ep)
    for c, w in weights.items():
        tgt = w * ep
        if tgt > 0:
            values[c] = tgt
            buys += max(0.0, tgt - current.get(c, 0.0))
    return values, traded * cost_rate, buys


def simulate(calendar, start, executions, opens, closes, cost_rate):
    """start 当日收盘仍为现金净值 1。executions 的键是开盘成交日。"""
    last_close = {}
    shares = {}
    cash = 1.0
    nav = []
    turnovers = []
    skips = []
    carried = 0
    open_marks = 0
    if start not in calendar:
        raise RuntimeError("起点不在交易日历里")
    start_i = calendar.index(start)

    def absorb_closes(date):
        for code, series in closes.items():
            px = series.get(date)
            if px is not None and px > 0:
                last_close[code] = px

    def mark(date):
        nonlocal carried, open_marks
        total = cash
        for code, sh in shares.items():
            px = closes.get(code, {}).get(date)
            if px is None or px <= 0:
                px = opens.get(code, {}).get(date)
                if px is not None and px > 0:
                    open_marks += 1
                else:
                    px = last_close.get(code)
                    if px is None:
                        raise RuntimeError("%s %s 无法计价" % (date, code))
                    carried += 1
            total += sh * px
        return total

    for i, date in enumerate(calendar):
        if i < start_i:
            absorb_closes(date)
            continue
        if date in executions:
            targets = list(executions[date])
            shares, cash, one_way = _rebalance(cash, shares, date, targets, opens, last_close, cost_rate, skips)
            if cash < -1e-6:
                raise RuntimeError("%s 再平衡后现金为负 %s" % (date, cash))
            if abs(cash) < 1e-10:
                cash = 0.0
            turnovers.append({"date": date, "one_way": one_way})
        if date == start and date not in executions:
            nav.append(1.0)
            absorb_closes(date)
            continue
        nav.append(mark(date))
        absorb_closes(date)
    dates = calendar[start_i:]
    if len(dates) != len(nav):
        raise RuntimeError("净值长度与交易日不一致")
    return {"dates": dates, "nav": nav, "turnovers": turnovers, "skips": skips,
            "carried_marks": carried, "open_marks": open_marks}


def sample_std(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return var ** 0.5


def weekly_returns(dates, nav):
    last_i = {}
    order = []
    for i, d in enumerate(dates):
        key = tuple(dt.date.fromisoformat(d).isocalendar()[:2])
        if key not in last_i:
            order.append(key)
        last_i[key] = i
    out = []
    prev = 0
    for key in order:
        i = last_i[key]
        if i == prev:
            continue
        out.append({"date": dates[i], "return": nav[i] / nav[prev] - 1.0})
        prev = i
    return out


def max_drawdown(dates, nav):
    peak = nav[0]
    peak_i = 0
    worst = 0.0
    trough_i = 0
    worst_peak = 0
    for i, v in enumerate(nav):
        if v >= peak:
            peak = v
            peak_i = i
        dd = v / peak - 1.0 if peak > 0 else 0.0
        if dd < worst:
            worst = dd
            trough_i = i
            worst_peak = peak_i
    recovery = None
    if worst < 0:
        peak_nav = nav[worst_peak]
        for i in range(trough_i + 1, len(nav)):
            if nav[i] >= peak_nav:
                recovery = dates[i]
                break
        return {"max_drawdown": worst, "peak": dates[worst_peak], "trough": dates[trough_i], "recovery": recovery}
    return {"max_drawdown": 0.0, "peak": None, "trough": None, "recovery": None}


def performance_stats(dates, nav, turnovers, rf=RISK_FREE):
    rets = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]
    years = (len(rets) / ANN_DAYS) if rets else None
    if not rets or nav[0] <= 0 or nav[-1] <= 0:
        ann = None
    else:
        ann = (nav[-1] / nav[0]) ** (ANN_DAYS / len(rets)) - 1.0
    vol_d = sample_std(rets)
    ann_vol = None if vol_d is None else vol_d * (ANN_DAYS ** 0.5)
    sharpe = None
    if vol_d not in (None, 0) and rets:
        sharpe = (sum(rets) / len(rets) - rf / ANN_DAYS) / vol_d * (ANN_DAYS ** 0.5)
    dd = max_drawdown(dates, nav)
    calmar = None
    if ann is not None and dd["max_drawdown"] < 0:
        calmar = ann / abs(dd["max_drawdown"])
    weeks = weekly_returns(dates, nav)
    wins = sum(1 for w in weeks if w["return"] > 0)
    one_ways = [t["one_way"] for t in turnovers]
    ann_turn = None if not years else sum(one_ways) / years
    return {
        "start": dates[0], "end": dates[-1], "trading_days": len(rets),
        "end_nav": nav[-1], "total_return": nav[-1] / nav[0] - 1.0,
        "ann_return": ann, "ann_vol": ann_vol, "sharpe": sharpe, "calmar": calmar,
        "max_drawdown": dd["max_drawdown"], "dd_peak": dd["peak"], "dd_trough": dd["trough"],
        "dd_recovery": dd["recovery"],
        "weekly_win_rate": (wins / len(weeks)) if weeks else None,
        "weekly_wins": wins, "weekly_count": len(weeks),
        "annual_turnover": ann_turn, "rebalance_count": len(turnovers),
        "weeks": weeks,
    }


def yearly_rows(dates, nav):
    last = {}
    for d, v in zip(dates, nav):
        last[d[:4]] = (d, v)
    prev = nav[0]
    start, end = dates[0], dates[-1]
    rows = []
    for y in sorted(last):
        d, v = last[y]
        partial = False
        note = "完整自然年"
        if y == start[:4] and start[5:7] != "01":
            partial = True
            note = "自样本起点"
        if y == end[:4] and end[5:7] != "12":
            partial = True
            note = "截至样本终点" if not (y == start[:4] and start[5:7] != "01") else "自样本起点，截至样本终点"
        rows.append({"year": int(y), "end": d, "return": v / prev - 1.0, "partial": partial, "note": note})
        prev = v
    return rows


def beat_rate(weeks, ref_weeks):
    ref = {w["date"]: w["return"] for w in ref_weeks}
    paired = [(w["return"], ref[w["date"]]) for w in weeks if w["date"] in ref]
    if not paired:
        return None, 0
    wins = sum(1 for a, b in paired if a > b)
    return wins / len(paired), len(paired)


def round_stats(s, beat=None):
    out = {
        "start": s["start"], "end": s["end"], "trading_days": s["trading_days"],
        "end_nav": r6(s["end_nav"]), "total_return": r6(s["total_return"]),
        "ann_return": r6(s["ann_return"]), "ann_vol": r6(s["ann_vol"]),
        "sharpe": r6(s["sharpe"]), "calmar": r6(s["calmar"]),
        "max_drawdown": r6(s["max_drawdown"]), "dd_peak": s["dd_peak"],
        "dd_trough": s["dd_trough"], "dd_recovery": s["dd_recovery"],
        "weekly_win_rate": r6(s["weekly_win_rate"]),
        "weekly_wins": s["weekly_wins"], "weekly_count": s["weekly_count"],
        "annual_turnover": r6(s["annual_turnover"]), "rebalance_count": s["rebalance_count"],
    }
    if beat is not None:
        out["weekly_beat_hs300"] = r6(beat[0])
        out["weekly_beat_count"] = beat[1]
    return out


def interior_gaps(bars, calendar):
    if not bars:
        return []
    have = {d for d, _, _ in bars}
    first, last = bars[0][0], bars[-1][0]
    return [d for d in calendar if first <= d <= last and d not in have]


def off_calendar(bars, calendar_set):
    return [d for d, _, _ in bars if d not in calendar_set]


def signal_dates(week_ends, freq):
    if freq == "weekly":
        return list(week_ends)
    if freq == "biweekly":
        return list(week_ends)[::2]
    raise ValueError(freq)


def build_rotation(signals, score_at, names, top_n, buf, next_day):
    prev = []
    execs = {}
    pending = None
    sized = []
    for sig in signals:
        rows = score_at(sig)
        codes, _, _ = fetch.choose_codes(rows, prev, top_n, buf, names)
        sized.append({"date": sig, "eligible": len(rows), "selected": len(codes)})
        nxt = next_day.get(sig)
        if not nxt:
            pending = sig
            break
        execs[nxt] = codes
        prev = codes
    return execs, pending, sized


def main():
    cfg = json.load(open(os.path.join(ROOT, "etfs.json"), encoding="utf-8"))
    rc = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    top_n = rc.get("top_n", cfg.get("top_n", 8))
    live_buf = rc["rebalance"]["buffer"]
    live_freq = rc["rebalance"]["freq"]
    names = {e["code"]: e["name"] for e in cfg["etfs"]}
    bench = cfg["benchmark"]
    today = dt.datetime.now(BJ).date()
    start = request_start(today)
    end = today.isoformat()
    print("请求区间 %s → %s" % (start, end), file=sys.stderr)

    # 本地重跑可设 BACKTEST_CACHE=1 复用 /tmp 里的行情。工作流不设这个变量，每次都重新下载。
    cached = None
    if os.environ.get("BACKTEST_CACHE") == "1" and os.path.exists(CACHE_PATH):
        try:
            blob = json.load(open(CACHE_PATH, encoding="utf-8"))
            if blob.get("start") == start and blob.get("end") == end:
                cached = blob
                print("使用本地行情缓存", file=sys.stderr)
        except Exception as e:
            print("忽略损坏的行情缓存: %s" % e, file=sys.stderr)
    if cached:
        index_bars = [tuple(x) for x in cached["index_bars"]]
        index_meta = cached["index_meta"]
        etf_bars = [tuple(x) for x in cached["etf_bars"]]
        etf_meta = cached["etf_meta"]
        pool = {c: [tuple(x) for x in rows] for c, rows in cached["pool"].items()}
        pool_meta = cached["pool_meta"]
        failed = cached["failed"]
    else:
        index_bars, index_meta = load_symbol(bench["code"], bench["market"], start, end)
        etf_bars, etf_meta = load_symbol(HS300_ETF["code"], HS300_ETF["market"], start, end)
        pool, pool_meta, failed = {}, {}, []
        for e in cfg["etfs"]:
            bars, meta = load_symbol(e["code"], e["market"], start, end)
            print("%s %s %s %s %s" % (e["code"], e["name"], meta.get("source"), len(bars), meta.get("note")), file=sys.stderr)
            if not bars:
                failed.append({"code": e["code"], "name": e["name"], "note": meta.get("note")})
                continue
            pool[e["code"]] = bars
            pool_meta[e["code"]] = meta
        json.dump({
            "start": start, "end": end,
            "index_bars": index_bars, "index_meta": index_meta,
            "etf_bars": etf_bars, "etf_meta": etf_meta,
            "pool": pool, "pool_meta": pool_meta, "failed": failed,
        }, open(CACHE_PATH, "w", encoding="utf-8"))
    if len(index_bars) < 61:
        raise SystemExit("沪深300指数日线不足，无法打分")
    if len(etf_bars) < 61:
        raise SystemExit("沪深300ETF 日线不足，无法做买入持有基准")
    if len(pool) < top_n:
        raise SystemExit("可用 ETF 少于 %d 只" % top_n)

    calendar = [d for d, _, _ in index_bars]
    cal_set = set(calendar)
    next_day = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    pos = {d: i for i, d in enumerate(calendar)}
    week_ends = [d for d in fetch.week_end_dates(calendar, today) if pos[d] >= 60]
    if not week_ends:
        raise SystemExit("没有足够长度的周末信号")
    month_ends = month_end_dates(calendar, today)

    opens = {}
    closes = {}
    for code, bars in pool.items():
        opens[code], closes[code] = price_maps(bars)
    opens[HS300_ETF["code"]], closes[HS300_ETF["code"]] = price_maps(etf_bars)

    score_cache = {}

    def score_at(signal):
        if signal in score_cache:
            return score_cache[signal]
        K = {}
        for e in cfg["etfs"]:
            bars = pool.get(e["code"])
            if not bars:
                continue
            cut = [(d, c) for d, _, c in bars if d <= signal]
            if cut:
                K[e["code"]] = cut
        bc = [c for d, _, c in index_bars if d <= signal]
        if len(bc) < 61:
            score_cache[signal] = []
            return []
        rows, _, _ = fetch.score_rows(cfg, K, bc)
        rows = [r for r in rows if r["date"] == signal]
        score_cache[signal] = rows
        return rows

    def score_tail(signal, nkeep=80):
        K = {}
        for e in cfg["etfs"]:
            bars = pool.get(e["code"])
            if not bars:
                continue
            cut = [(d, c) for d, _, c in bars if d <= signal][-nkeep:]
            if cut:
                K[e["code"]] = cut
        bc = [c for d, _, c in index_bars if d <= signal][-nkeep:]
        rows, _, _ = fetch.score_rows(cfg, K, bc)
        return {r["code"]: r["score"] for r in rows if r["date"] == signal}

    for probe in (calendar[-1], week_ends[0]):
        full = {r["code"]: r["score"] for r in score_at(probe)}
        if full != score_tail(probe):
            raise SystemExit("近端截断后得分变化，打分输入与全历史不一致：" + probe)

    def run_variant(freq, buf):
        signals = signal_dates(week_ends, freq)
        execs, pending, sized = build_rotation(signals, score_at, names, top_n, buf, next_day)
        sim = simulate(calendar, week_ends[0], execs, opens, closes, ONE_WAY_COST)
        nav = [round(v, 6) for v in sim["nav"]]
        stats = performance_stats(sim["dates"], nav, sim["turnovers"])
        return sim, nav, stats, pending, sized

    sim_s, nav_s, stats_s, pending, sized = run_variant(live_freq, live_buf)

    first_exec = next_day.get(week_ends[0])
    if not first_exec or first_exec not in opens[HS300_ETF["code"]]:
        raise SystemExit("沪深300ETF 在首次成交日没有开盘价")

    def universe(sig):
        return [e["code"] for e in cfg["etfs"] if e["code"] in closes and sig in closes[e["code"]]]

    eq_exec = {first_exec: universe(week_ends[0])}
    for m in month_ends:
        if m <= week_ends[0]:
            continue
        nxt = next_day.get(m)
        if not nxt or nxt == first_exec:
            continue
        eq_exec[nxt] = universe(m)
    sim_e = simulate(calendar, week_ends[0], eq_exec, opens, closes, ONE_WAY_COST)
    nav_e = [round(v, 6) for v in sim_e["nav"]]
    stats_e = performance_stats(sim_e["dates"], nav_e, sim_e["turnovers"])

    bh_exec = {first_exec: [HS300_ETF["code"]]}
    sim_b = simulate(calendar, week_ends[0], bh_exec, opens, closes, ONE_WAY_COST)
    nav_b = [round(v, 6) for v in sim_b["nav"]]
    stats_b = performance_stats(sim_b["dates"], nav_b, sim_b["turnovers"])

    beat_s = beat_rate(stats_s["weeks"], stats_b["weeks"])
    beat_e = beat_rate(stats_e["weeks"], stats_b["weeks"])
    dates = sim_s["dates"]
    if dates != sim_b["dates"] or dates != sim_e["dates"]:
        raise RuntimeError("三条净值的日期没有对齐")

    sens_rows = []
    for freq in ("weekly", "biweekly"):
        for buf in (0, 5, 10):
            if freq == live_freq and buf == live_buf:
                st = stats_s
                live = True
            else:
                _, _, st, _, _ = run_variant(freq, buf)
                live = False
            sens_rows.append({
                "buffer": buf,
                "freq": freq,
                "freq_label": "每周" if freq == "weekly" else "双周",
                "live": live,
                **{k: round_stats(st)[k] for k in (
                    "ann_return", "ann_vol", "max_drawdown", "sharpe", "calmar",
                    "weekly_win_rate", "weekly_wins", "weekly_count", "annual_turnover",
                    "end_nav", "rebalance_count", "total_return")},
            })

    y_s = yearly_rows(dates, nav_s)
    y_b = {r["year"]: r for r in yearly_rows(dates, nav_b)}
    y_e = {r["year"]: r for r in yearly_rows(dates, nav_e)}
    yearly = []
    for r in y_s:
        yearly.append({
            "year": r["year"], "partial": r["partial"], "note": r["note"], "end": r["end"],
            "strategy": r6(r["return"]),
            "hs300": r6(y_b[r["year"]]["return"]),
            "equal": r6(y_e[r["year"]]["return"]),
        })

    def symbol_row(code, name, role, bars, meta):
        gaps = interior_gaps(bars, calendar)
        off = off_calendar(bars, cal_set)
        return {
            "code": code, "name": name, "role": role,
            "source": meta.get("source"), "adjust": meta.get("adjust"),
            "first": bars[0][0], "last": bars[-1][0], "bars": len(bars),
            "bad_ticks": meta.get("bad_ticks") or 0,
            "interior_gaps": len(gaps), "gap_examples": gaps[:8],
            "off_calendar": len(off), "note": meta.get("note") or "",
        }

    symbols = [
        symbol_row(bench["code"], bench["name"], "score_index", index_bars, index_meta),
        symbol_row(HS300_ETF["code"], HS300_ETF["name"], "benchmark", etf_bars, etf_meta),
    ]
    for e in cfg["etfs"]:
        if e["code"] not in pool:
            continue
        symbols.append(symbol_row(e["code"], e["name"], "pool", pool[e["code"]], pool_meta[e["code"]]))

    latest_path = os.path.join(ROOT, "data", "latest.json")
    score_check = {"status": "skipped", "mismatches": []}
    if os.path.exists(latest_path):
        latest = json.load(open(latest_path, encoding="utf-8"))
        day = latest.get("data_date")
        if day not in pos:
            score_check = {"status": "数据日不在行情里", "data_date": day, "mismatches": []}
        else:
            rows = score_at(day)
            mine = {r["code"]: r["score"] for r in rows}
            mismatches = []
            for r in latest.get("rows") or []:
                got = mine.get(r["code"])
                if got is None or abs(got - r["score"]) > 0.11:
                    mismatches.append({"code": r["code"], "latest": r["score"], "backtest": got})
            worst = 0.0
            for r in latest.get("rows") or []:
                got = mine.get(r["code"])
                if got is not None:
                    worst = max(worst, abs(got - r["score"]))
            score_check = {
                "status": "ok" if not mismatches else "snapshot_diff",
                "data_date": day,
                "compared": len(latest.get("rows") or []),
                "mismatches": mismatches,
                "max_abs_diff": r6(worst),
                "note": (
                    "与仓库里已保存的 latest.json 得分一致。"
                    if not mismatches else
                    "latest.json 是上次抓数时的前复权快照。除权之后前复权会改写更早的收盘，所以之后再下载，同一天的得分可以不同。回测打分调用 fetch.score_rows。"
                ),
            }

    skips = sim_s["skips"] + sim_b["skips"] + sim_e["skips"]
    coverage = {
        "request_start": start, "request_end": end,
        "price_start": calendar[0], "price_end": calendar[-1],
        "backtest_start": dates[0], "backtest_end": dates[-1],
        "first_execution": first_exec,
        "score_index": {"code": bench["code"], "name": bench["name"]},
        "eligible_on_first_signal": sized[0]["eligible"] if sized else None,
        "eligible_on_last_signal": sized[-1]["eligible"] if sized else None,
        "pending_signal": pending,
        "failed": failed,
        "symbols": symbols,
        "trade_skip_count": len(skips),
        "trade_skips": skips[:40],
        "carried_marks": sim_s["carried_marks"] + sim_b["carried_marks"] + sim_e["carried_marks"],
        "open_marks": sim_s["open_marks"] + sim_b["open_marks"] + sim_e["open_marks"],
        "score_check": score_check,
    }

    metrics = {
        "strategy": round_stats(stats_s, beat_s),
        "hs300": round_stats(stats_b),
        "equal": round_stats(stats_e, beat_e),
    }

    out = {
        "disclaimer": DISCLAIMER,
        "generated_at": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M"),
        "in_sample": True,
        "sample_note": SAMPLE_NOTE,
        "biweekly_note": BIWEEKLY_NOTE,
        "rules": {
            "top_n": top_n, "freq": live_freq, "buffer": live_buf,
            "score": fetch.SCORE_RULE,
            "signal": "每周最后一个交易日收盘出信号，下一交易日开盘价成交",
            "buffer_rule": "新入选者得分须高于被替换在位者得分 + 缓冲，比较为严格大于",
            "weight": "入选名单等权，得分只用于选择",
            "start": "样本内第一个可打分的周末从现金起步，不读取、不修改正式名单",
            "score_benchmark": bench["code"],
        },
        "costs": {
            "commission": COMMISSION, "slippage": SLIPPAGE,
            "commission_label": "单边万分之三", "slippage_label": "千分之一",
            "note": "买入和卖出各按成交额扣除一次佣金和一次滑点。成交额按开盘价计算。",
        },
        "risk_free_rate": RISK_FREE,
        "trading_days_per_year": ANN_DAYS,
        "benchmarks": [
            {"id": "hs300", "name": "沪深300ETF买入持有", "code": "510300"},
            {"id": "equal", "name": "池内等权月度再平衡", "note": "每月最后一个交易日收盘确定名单，下一交易日开盘再平衡。上市前没有日线的ETF不参与。"},
        ],
        "definitions": {
            "ann_return": "期末净值相对起点的复利，按 252 个交易日折到一年",
            "ann_vol": "日简单收益的样本标准差 × √252",
            "sharpe": "（日收益均值 − 2%/252）/ 日收益样本标准差 × √252",
            "calmar": "年化收益 / 最大回撤绝对值",
            "max_drawdown": "净值从前期高点到之后低点的最大跌幅，区间给出高点日与低点日",
            "weekly_win_rate": "按 ISO 周取该周最后一个交易日净值，周收益为正的周数占比",
            "weekly_beat_hs300": "同一周收益高于沪深300ETF买入持有的周数占比",
            "annual_turnover": "各次调仓买入额 / 调仓前权益 之和，除以年数（252 个交易日为一年），含首次建仓",
        },
        "coverage": coverage,
        "curves": {
            "dates": dates,
            "series": [
                {"id": "strategy", "name": "八友轮动", "nav": nav_s},
                {"id": "hs300", "name": "沪深300ETF买入持有", "nav": nav_b},
                {"id": "equal", "name": "池内等权月度再平衡", "nav": nav_e},
            ],
        },
        "metrics": metrics,
        "yearly": yearly,
        "sensitivity": {"note": SAMPLE_NOTE, "biweekly_note": BIWEEKLY_NOTE, "rows": sens_rows},
    }
    for series in (nav_s, nav_b, nav_e):
        if any(v != v or v <= 0 for v in series):
            raise SystemExit("净值出现非正数，拒绝写出结果")
    print_report(out)
    path = os.path.join(ROOT, "data", "backtest.json")
    tmp = path + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def print_report(out):
    m = out["metrics"]
    cov = out["coverage"]
    print("回测区间 %s → %s" % (cov["backtest_start"], cov["backtest_end"]))
    print("行情 %s → %s，首次成交 %s" % (cov["price_start"], cov["price_end"], cov["first_execution"]))

    def line(title, s):
        def pct(x):
            return "—" if x is None else "%+.2f%%" % (x * 100)
        def n(x):
            return "—" if x is None else "%+.2f" % x
        dd = pct(s["max_drawdown"])
        if s.get("dd_peak"):
            dd += " (%s → %s)" % (s["dd_peak"], s["dd_trough"])
        beat = s.get("weekly_beat_hs300")
        print("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            title, pct(s["ann_return"]), pct(s["ann_vol"]), dd, n(s["sharpe"]), n(s["calmar"]),
            "—" if s["weekly_win_rate"] is None else "%.2f%% (%d/%d)" % (s["weekly_win_rate"] * 100, s["weekly_wins"], s["weekly_count"]),
            "—" if s["annual_turnover"] is None else "%.2f" % s["annual_turnover"],
        ))
        if beat is not None:
            print("  跑赢沪深300ETF周占比 %.2f%%" % (beat * 100))

    print("| 组合 | 年化收益 | 年化波动 | 最大回撤 | 夏普 | 卡玛 | 周胜率 | 年换手 |")
    line("八友轮动", m["strategy"])
    line("沪深300ETF", m["hs300"])
    line("池内等权", m["equal"])
    print("年度收益")
    for r in out["yearly"]:
        print("%s %s %+.2f%% / %+.2f%% / %+.2f%% %s" % (
            r["year"], r["note"], r["strategy"] * 100, r["hs300"] * 100, r["equal"] * 100, r["end"]))
    print("敏感性")
    for r in out["sensitivity"]["rows"]:
        print("缓冲%s %s 年化%s 回撤%s 夏普%s 换手%s%s" % (
            r["buffer"], r["freq_label"],
            "—" if r["ann_return"] is None else "%+.2f%%" % (r["ann_return"] * 100),
            "—" if r["max_drawdown"] is None else "%+.2f%%" % (r["max_drawdown"] * 100),
            "—" if r["sharpe"] is None else "%+.2f" % r["sharpe"],
            "—" if r["annual_turnover"] is None else "%.2f" % r["annual_turnover"],
            " 实盘" if r["live"] else ""))
    failed = cov["failed"]
    print("缺失代码: %s" % (failed or "无"))
    gaps = [s for s in cov["symbols"] if s["interior_gaps"] or s["off_calendar"] or s["bad_ticks"] or s["adjust"] != "qfq"]
    if not gaps:
        print("区间内无内部缺口，复权均为前复权")
    else:
        for s in gaps:
            print("数据 %s %s src=%s adjust=%s gaps=%s %s off=%s bad=%s %s" % (
                s["code"], s["name"], s["source"], s["adjust"], s["interior_gaps"],
                "、".join(s["gap_examples"]), s["off_calendar"], s["bad_ticks"], s["note"]))
    late = [s for s in cov["symbols"] if s["role"] == "pool" and s["first"] > cov["price_start"]]
    for s in late:
        print("较晚进入 %s %s 首根 %s" % (s["code"], s["name"], s["first"]))
    print("跳过成交 %s 笔，沿用最近价格计价 %s 次" % (cov["trade_skip_count"], cov["carried_marks"]))
    print("打分对照", cov["score_check"])


if __name__ == "__main__":
    main()
