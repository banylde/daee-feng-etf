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

def main():
    cfg = json.load(open(os.path.join(ROOT, "etfs.json"), encoding="utf-8"))
    top_n = cfg.get("top_n", 8)
    b = cfg["benchmark"]
    bk, bsrc = fetch(b["code"], b["market"])
    if not bk:
        sys.exit("基准抓取失败")
    bc = [x[1] for x in bk]
    b20, b60 = ret(bc, 20), ret(bc, 60)
    rows, failed, sources = [], [], {bsrc}
    for e in cfg["etfs"]:
        k, src = fetch(e["code"], e["market"])
        if not k:
            failed.append(e["code"]); continue
        sources.add(src)
        c = [x[1] for x in k]
        r20, r60 = ret(c, 20), ret(c, 60)
        rows.append({**e, "date": k[-1][0], "close": c[-1],
                     "r5": round(ret(c, 5), 2), "r20": round(r20, 2), "r60": round(r60, 2),
                     "ex20": round(r20 - b20, 2), "ex60": round(r60 - b60, 2),
                     "above20": c[-1] > ma(c, 20), "above60": c[-1] > ma(c, 60), "src": src})
        time.sleep(0.3)
    if not rows:
        sys.exit("全部失败")
    # 20日动量排名（1=最强）
    for i, r in enumerate(sorted(rows, key=lambda x: -x["r20"])):
        r["mom_rank"] = i + 1
    # 综合得分：各指标在池内做百分位(0-100)后加权，再加均线分
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
    for i, r in enumerate(rows):
        r["rank"] = i + 1; r["top"] = i < top_n
    path = os.path.join(ROOT, "data", "latest.json")
    prev_top, prev_date = [], None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
            if old.get("data_date") != rows[0]["date"]:
                prev_top, prev_date = old.get("top_codes", []), old.get("data_date")
            else:
                prev_top, prev_date = old.get("prev_top_codes", []), old.get("prev_date")
        except Exception:
            pass
    top = [r["code"] for r in rows if r["top"]]
    out = {
        "updated_at": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M"),
        "data_date": max(r["date"] for r in rows),
        "benchmark": {**b, "r20": round(b20, 2), "r60": round(b60, 2)},
        "sources": sorted(sources), "failed": failed, "top_n": top_n,
        "top_codes": top, "prev_top_codes": prev_top, "prev_date": prev_date,
        "entered": [c for c in top if prev_top and c not in prev_top],
        "exited": [c for c in prev_top if c not in top],
        "rule": "得分=0.4×20日超额百分位+0.3×60日超额百分位+0.1×5日涨幅百分位+站上MA20加10+站上MA60加10（满分100）",
        "rows": rows,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"OK {len(rows)} 只, 失败 {failed}, 源 {sorted(sources)}, 前{top_n}: {top}")

if __name__ == "__main__":
    main()
