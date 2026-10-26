# 风部 · 八友御风 ETF轮动看板

“大鹅基金”风部（20%）的行业ETF轮动研究工具：纯静态 PWA + GitHub Actions 每日抓数。
**仅为个人研究工具，不构成投资建议。** 仓库内无任何持仓/账户/收益数据，也无任何密钥。

## 结构
- `etfs.json`：ETF池与基准（沪深300）、`top_n`（默认8）
- `scripts/fetch.py`：抓日线、算指标，写 `data/latest.json`（仅用 Python 标准库）
- `.github/workflows/update.yml`：周一至周五北京时间 15:40 自动运行（也可在 Actions 页手动 Run workflow）
- `index.html` / `manifest.webmanifest` / `sw.js` / `icons/`：前端与 PWA

## 数据源
腾讯财经日K（前复权）为主；失败时依次回退东方财富（前复权）、新浪（不复权）。均为免费公开接口，可能变更或限流；失败的代码会显示在页面顶部。

## 指标口径（均基于收盘价，前复权）
- 5/20/60日涨幅：最新收盘 / N个交易日前收盘 − 1
- 20/60日超额：ETF同期涨幅 − 沪深300同期涨幅（百分点）
- 站上MA20/MA60：最新收盘 > 近20/60日收盘均值
- 20日动量排名：按20日涨幅在池内排序，1=最强
- **综合得分（0–100）** = 0.4×20日超额百分位 + 0.3×60日超额百分位 + 0.1×5日涨幅百分位 + 站上MA20加10 + 站上MA60加10
  （百分位=池内相对排名，0最弱、100最强）。按得分排序，前8名为“当期八友”。
- 进入/退出：与上一个**数据日期**的前8名对比。权重可在 `fetch.py` 中修改。

## 修改ETF池
编辑 `etfs.json` 的 `etfs` 数组：`code`（6位代码）、`market`（`sh` 上交所 / `sz` 深交所）、`name`、`sector`。提交后在 Actions 手动运行一次即可刷新。本地：`python3 scripts/fetch.py`。

## Vercel 部署
1. Vercel → Add New → Project → Import 本 GitHub 仓库（需授权 Vercel 访问该私有仓库）
2. Framework Preset 选 **Other**；Build Command 留空；Output Directory 留空（根目录即静态目录）
3. Deploy。之后 Actions 每次提交 `data/latest.json` 会自动触发重新部署。
4. 手机浏览器打开站点 →“添加到主屏幕”。
