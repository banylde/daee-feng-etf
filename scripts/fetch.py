#!/usr/bin/env python3
"""抓取ETF日线（腾讯前复权为主，东方财富前复权/新浪不复权为备），计算轮动指标，写 data/latest.json。

数据日期早于最近已收盘的 A 股交易日，或主源与备用都抓不到时，把 latest.json 标为 stale，不静默沿用旧数字。
正式调仓写入 data/official.json，并追加 data/history.json（已有日期不改、不补写未知成交）。
"""
import json, os, re, sys, time, datetime as dt
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
BJ = dt.timezone(dt.timedelta(hours=8))
CLOSE = dt.time(15, 0)
PRIMARY = "tencent"

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "ignore")

def em(code, market, n=150):
    secid = ("1." if market == "sh" else "0.") + code
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3"
           "&fields2=f51,f53&klt=101&fqt=1&end=20500101&lmt=%d&ut=fa5fd1943c7b386f172d6893dbfba10b" % (secid, n))
    d = json.loads(get(url))["data"]
    return [(k.split(",")[0], float(k.split(",")[1])) for k in d["klines"]], d.get("name")

def sina(code, market, n=150):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
           "?symbol=%s%s&scale=240&ma=no&datalen=%d" % (market, code, n))
    return [(x["day"], float(x["close"])) for x in json.loads(get(url))], None

def tencent(code, market, n=150):
    sym = market + code
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (sym, n)
    d = json.loads(get(url))["data"][sym]
    k = d.get("qfqday") or d.get("day")
    return [(x[0], float(x[2])) for x in k], None

def previous_weekday(d):
    d = d - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d

def calendar_completed_day(now):
    """行情接口都不可用时的工作日近似。节假日可能把未收盘日当成应有数据日。"""
    d = now.date()
    if d.weekday() >= 5:
        return d - dt.timedelta(days=d.weekday() - 4)
    if now.time() < CLOSE:
        return previous_weekday(d)
    return d

def completed_session(qdate, qtime, now):
    """行情时间戳对应的最近一个已收盘交易日。"""
    if qtime >= CLOSE or (now.date() == qdate and now.time() >= CLOSE):
        return qdate
    return previous_weekday(qdate)

def tencent_quote_clock():
    raw = get("https://qt.gtimg.cn/q=sh000001")
    found = re.findall(r"(20\d{12})", raw)
    if not found:
        raise ValueError("腾讯行情无时间")
    return dt.datetime.strptime(found[-1], "%Y%m%d%H%M%S")

def sina_quote_clock():
    req = urllib.request.Request(
        "https://hq.sinajs.cn/list=sh000001",
        headers={"User-Agent": UA["User-Agent"], "Referer": "https://finance.sina.com.cn"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("gbk", "ignore")
    m = re.search(r"(\d{4}-\d{2}-\d{2}),(\d{2}:\d{2}:\d{2})", raw)
    if not m:
        raise ValueError("新浪行情无时间")
    return dt.datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")

def eastmoney_completed(now):
    k, _ = em("000001", "sh", 5)
    last = dt.date.fromisoformat(k[-1][0])
    if last == now.date() and now.time() < CLOSE:
        return previous_weekday(last)
    return last

def latest_completed_trading_day(now=None):
    """最近一个已收盘的 A 股交易日。返回 (date, source)。不编造行情。"""
    now = now or dt.datetime.now(BJ)
    now = now.replace(tzinfo=BJ) if now.tzinfo is None else now.astimezone(BJ)
    def tencent_day():
        q = tencent_quote_clock()
        return completed_session(q.date(), q.time(), now)
    def sina_day():
        q = sina_quote_clock()
        return completed_session(q.date(), q.time(), now)
    for name, fn in (("tencent-quote", tencent_day), ("sina-quote", sina_day), ("eastmoney-kline", lambda: eastmoney_completed(now))):
        try:
            return fn(), name
        except Exception as e:
            print(f"[{name}] 交易日核对失败: {e}", file=sys.stderr)
    return calendar_completed_day(now), "weekday-calendar"

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def flag_existing_stale(reason):
    """抓取失败时保留已有数字，并明确标过期。没有旧文件就不写行情。"""
    path = os.path.join(ROOT, "data", "latest.json")
    out = {}
    if os.path.exists(path):
        try:
            out = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"读取 latest.json 失败: {e}", file=sys.stderr)
            out = {}
    expected, source = latest_completed_trading_day()
    out["stale"] = True
    out["stale_reason"] = reason
    out["expected_trading_day"] = expected.isoformat()
    out["trading_day_source"] = source
    out["updated_at"] = dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    out.setdefault("data_date", None)
    out.setdefault("failed", [])
    out.setdefault("sources", [])
    out.setdefault("fallbacks", [])
    out.setdefault("rows", [])
    out.setdefault("top_codes", [])
    out.setdefault("cand_codes", [])
    out.setdefault("prev_top_codes", [])
    out.setdefault("entered", [])
    out.setdefault("exited", [])
    out.setdefault("top_n", 8)
    out.setdefault("official", {})
    out.setdefault("rebalance", {})
    out.setdefault("rule", "")
    out.setdefault("benchmark", {"name": "沪深300", "code": "000300", "market": "sh", "r20": None, "r60": None})
    write_json(path, out)
    print(f"STALE {reason}", file=sys.stderr)
    return out

def history_event(OF, names):
    codes = list(OF.get("codes") or [])
    prev = list(OF.get("prev_codes") or [])
    scores = OF.get("scores") or {}
    weight = f"1/{len(codes)}" if codes else None
    def named(c):
        return {"code": c, "name": names.get(c) or c}
    holdings = []
    for c in codes:
        row = {**named(c), "weight": weight}
        if c in scores:
            row["score"] = scores[c]
        holdings.append(row)
    changes = list(OF.get("changes") or [])
    return {
        "date": OF["rebalance_date"],
        "computed_at": OF.get("computed_at"),
        "changes": changes,
        "reason": "；".join(changes) if changes else "无变动",
        "entered": [named(c) for c in codes if c not in prev],
        "exited": [named(c) for c in prev if c not in codes],
        "holdings": holdings,
    }

def append_history(OF, names):
    """只追加当前 official 里尚无记录的调仓日，不改已有条目，不编造更早的成交。"""
    if not OF or not OF.get("rebalance_date"):
        return
    path = os.path.join(ROOT, "data", "history.json")
    if os.path.exists(path):
        hist = json.load(open(path, encoding="utf-8"))
    else:
        hist = {
            "weight_basis": "equal",
            "weight_note": "正式名单等权，每只权重为 1/只数。得分只用于选择，不是仓位。",
            "events": [],
        }
    events = hist.setdefault("events", [])
    if any(e.get("date") == OF["rebalance_date"] for e in events):
        return
    hist.setdefault("weight_basis", "equal")
    hist.setdefault("weight_note", "正式名单等权，每只权重为 1/只数。得分只用于选择，不是仓位。")
    events.append(history_event(OF, names))
    write_json(path, hist)

def fetch(code, market):
    for name, fn in (("tencent", tencent), ("eastmoney", em), ("sina", sina)):
        for _ in range(2):
            try:
                k, nm = fn(code, market)
                if len(k) >= 61:
                    return k, name
            except Exception as e:
                print(f"[{name}] {code} 失败: {e}", file=sys.stderr)
            time.sleep(1)
    return None, None

def ret(c, n):
    return (c[-1] / c[-1 - n] - 1) * 100

def ma(c, n):
    return sum(c[-n:]) / n

def score_rows(cfg, K, bc):
    """按给定收盘序列（已截到同一日期）计算得分与排名。"""
    b20, b60 = ret(bc, 20), ret(bc, 60)
    rows = []
    for e in cfg["etfs"]:
        k = K.get(e["code"])
        if not k or len(k) < 61: continue
        c = [x[1] for x in k]; r20, r60 = ret(c, 20), ret(c, 60)
        rows.append({**e, "date": k[-1][0], "close": c[-1],
                     "r5": round(ret(c, 5), 2), "r20": round(r20, 2), "r60": round(r60, 2),
                     "ex20": round(r20 - b20, 2), "ex60": round(r60 - b60, 2),
                     "above20": c[-1] > ma(c, 20), "above60": c[-1] > ma(c, 60), "src": e.get("_src")})
    for i, r in enumerate(sorted(rows, key=lambda x: -x["r20"])):
        r["mom_rank"] = i + 1
    def pct(key):
        s = sorted(r[key] for r in rows); n = len(s)
        for r in rows:
            r["_p_" + key] = 100 * s.index(r[key]) / (n - 1) if n > 1 else 50
    for key in ("r5", "ex20", "ex60"):
        pct(key)
    for r in rows:
        r["score"] = round(0.40 * r["_p_ex20"] + 0.30 * r["_p_ex60"] + 0.10 * r["_p_r5"]
                           + 10 * r["above20"] + 10 * r["above60"], 1)
        for key in list(r):
            if key.startswith("_p_"): del r[key]
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows): r["rank"] = i + 1
    return rows, b20, b60

def last_week_end(dates, today):
    """最近一个已结束周的最后交易日：周五，或其后已出现下一周的交易日/日历已进入下一周。"""
    D = [dt.date.fromisoformat(d) for d in dates]; wk = lambda d: d.isocalendar()[:2]
    for i in range(len(D) - 1, -1, -1):
        d = D[i]; nxt = D[i + 1] if i + 1 < len(D) else None
        if d.weekday() == 4 or (nxt and wk(nxt) > wk(d)) or (not nxt and wk(today) > wk(d)):
            return d.isoformat()
    return None

def main():
    cfg = json.load(open(os.path.join(ROOT, "etfs.json"), encoding="utf-8"))
    rc = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    top_n = rc.get("top_n", cfg.get("top_n", 8)); rb = rc["rebalance"]; buf = rb["buffer"]
    b = cfg["benchmark"]
    bk, bsrc = fetch(b["code"], b["market"])
    if not bk:
        flag_existing_stale("基准抓取失败，沿用上次结果")
        sys.exit(1)
    K, failed, sources, fallbacks = {}, [], {bsrc}, []
    if bsrc != PRIMARY:
        fallbacks.append({"code": b["code"], "name": b["name"], "source": bsrc})
    for e in cfg["etfs"]:
        k, src = fetch(e["code"], e["market"])
        if not k:
            failed.append(e["code"]); continue
        sources.add(src); K[e["code"]] = k; e["_src"] = src
        if src != PRIMARY:
            fallbacks.append({"code": e["code"], "name": e["name"], "source": src})
        time.sleep(0.3)
    bc = [x[1] for x in bk]
    rows, b20, b60 = score_rows(cfg, K, bc)
    if not rows:
        flag_existing_stale("全部标的抓取失败，沿用上次结果")
        sys.exit(1)
    for e in cfg["etfs"]: e.pop("_src", None)
    for r in rows: r.pop("_src", None)
    # 正式名单：每周最后一个交易日收盘数据调仓一次，其间冻结；挑战者得分须高出被替换者>buffer才替换
    opath = os.path.join(ROOT, "data", "official.json")
    OF = json.load(open(opath, encoding="utf-8")) if os.path.exists(opath) else {}
    today = dt.datetime.now(BJ).date()
    R = last_week_end([x[0] for x in bk], today)
    if R and (not OF.get("rebalance_date") or OF["rebalance_date"] < R):
        cut = lambda k: [x for x in k if x[0] <= R]
        rrows, _, _ = score_rows(cfg, {c: cut(k) for c, k in K.items()}, [x[1] for x in cut(bk)])
        rrows = [r for r in rrows if r["date"] == R]  # 仅用调仓日有收盘价的ETF
        sc = {r["code"]: r["score"] for r in rrows}; nm = {e["code"]: e["name"] for e in cfg["etfs"]}
        inc = OF.get("codes", []); changes = []
        kept = [c for c in inc if c in sc]
        changes += [f"{nm.get(c, c)} 调仓日无数据→移出" for c in inc if c not in sc]
        for r in rrows:
            if len(kept) >= top_n: break
            if r["code"] not in kept: kept.append(r["code"]); changes.append(f'{r["name"]} 补入空位' if inc else f'{r["name"]} 首期入选')
        for _ in range(top_n * 3):
            ch = [r["code"] for r in rrows if r["code"] not in kept]
            if not ch: break
            c = ch[0]; w = min(kept, key=lambda x: sc[x])
            if sc[c] > sc[w] + buf:
                kept[kept.index(w)] = c; changes.append(f"{nm[c]}({sc[c]}) 替换 {nm[w]}({sc[w]})")
            else: break
        kept.sort(key=lambda c: -sc[c])
        Rd = dt.date.fromisoformat(R); nxt = Rd + dt.timedelta(days=(4 - Rd.weekday()) % 7 or 7)
        OF = {"rebalance_date": R, "next_rebalance": nxt.isoformat(), "freq": rb["freq"], "buffer": buf,
              "codes": kept, "scores": {c: sc[c] for c in kept}, "prev_codes": inc, "prev_rebalance_date": OF.get("rebalance_date"),
              "changes": changes, "computed_at": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")}
        json.dump(OF, open(opath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    names = {e["code"]: e["name"] for e in cfg["etfs"]}
    append_history(OF, names)
    top = OF["codes"]; prev_top = OF.get("prev_codes", [])
    cand = [r["code"] for r in rows[:top_n]]
    for r in rows:
        r["top"] = r["code"] in top; r["cand"] = r["code"] in cand
    path = os.path.join(ROOT, "data", "latest.json")
    data_date = max(r["date"] for r in rows)
    expected, tsrc = latest_completed_trading_day()
    exp = expected.isoformat()
    stale = data_date < exp
    out = {
        "updated_at": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M"),
        "data_date": data_date,
        "stale": stale,
        "stale_reason": f"数据日期 {data_date} 早于最近A股交易日 {exp}（{tsrc}）" if stale else "",
        "expected_trading_day": exp,
        "trading_day_source": tsrc,
        "benchmark": {**b, "r20": round(b20, 2), "r60": round(b60, 2)},
        "sources": sorted(sources), "fallbacks": fallbacks, "failed": failed, "top_n": top_n,
        "top_codes": top, "cand_codes": cand, "prev_top_codes": prev_top, "prev_date": OF.get("prev_rebalance_date"),
        "entered": [c for c in top if prev_top and c not in prev_top],
        "exited": [c for c in prev_top if c not in top],
        "official": {k: OF.get(k) for k in ("rebalance_date", "next_rebalance", "freq", "buffer", "changes", "scores")},
        "rebalance": rb,
        "rule": "得分=0.4×20日超额百分位+0.3×60日超额百分位+0.1×5日涨幅百分位+站上MA20加10+站上MA60加10（满分100）",
        "rows": rows,
    }
    write_json(path, out)
    print(f"OK {len(rows)} 只, 失败 {failed}, 源 {sorted(sources)}, 回退 {fallbacks}, 正式({OF['rebalance_date']}): {top}, 候选: {cand}, stale={stale}")
    if stale:
        sys.exit(1)

if __name__ == "__main__":
    main()
