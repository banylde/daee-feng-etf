#!/usr/bin/env python3
"""抓取ETF日线（腾讯前复权为主，东方财富前复权/新浪不复权为备），计算轮动指标，写 data/latest.json。"""
import json, os, sys, time, datetime as dt
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
BJ = dt.timezone(dt.timedelta(hours=8))

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
        sys.exit("基准抓取失败")
    K, failed, sources = {}, [], {bsrc}
    for e in cfg["etfs"]:
        k, src = fetch(e["code"], e["market"])
        if not k:
            failed.append(e["code"]); continue
        sources.add(src); K[e["code"]] = k; e["_src"] = src
        time.sleep(0.3)
    bc = [x[1] for x in bk]
    rows, b20, b60 = score_rows(cfg, K, bc)
    if not rows:
        sys.exit("全部失败")
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
    top = OF["codes"]; prev_top = OF.get("prev_codes", [])
    cand = [r["code"] for r in rows[:top_n]]
    for r in rows:
        r["top"] = r["code"] in top; r["cand"] = r["code"] in cand
    path = os.path.join(ROOT, "data", "latest.json")
    out = {
        "updated_at": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M"),
        "data_date": max(r["date"] for r in rows),
        "benchmark": {**b, "r20": round(b20, 2), "r60": round(b60, 2)},
        "sources": sorted(sources), "failed": failed, "top_n": top_n,
        "top_codes": top, "cand_codes": cand, "prev_top_codes": prev_top, "prev_date": OF.get("prev_rebalance_date"),
        "entered": [c for c in top if prev_top and c not in prev_top],
        "exited": [c for c in prev_top if c not in top],
        "official": {k: OF.get(k) for k in ("rebalance_date", "next_rebalance", "freq", "buffer", "changes", "scores")},
        "rebalance": rb,
        "rule": "得分=0.4×20日超额百分位+0.3×60日超额百分位+0.1×5日涨幅百分位+站上MA20加10+站上MA60加10（满分100）",
        "rows": rows,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"OK {len(rows)} 只, 失败 {failed}, 源 {sorted(sources)}, 正式({OF['rebalance_date']}): {top}, 候选: {cand}")

if __name__ == "__main__":
    main()
