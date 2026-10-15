# 离线回测

实时脚本每次只向 OKX `/api/v5/market/candles` 要最近约 100 根已收盘 K 线。`data/neff_history.csv` 是这些读数的稀疏快照，没有原始 OHLC，不能当作回测样本。这里另做一条离线链路：回补历史 K 线，对每一根已收盘 K 线调用同一个 `analyze()`，再和已有快照对账，并给出第一张描述性的表。

它不回答「该不该买」。N_eff 是走完之后的几何描述，下面的表只比较下一根 K 线的绝对波动。

定时任务 `.github/workflows/neff.yml` 不跑这些脚本。

## 和实盘同一套数学

- κ、N_eff、C、c_ratio、ER、state、σ 全部来自 `neff.analyze`。回放没有第二套公式。
- `WINDOW`（默认 60）和 `SMOOTH`（默认 5）跟实盘一样，从环境变量读。对账时必须和写历史时一致。
- 居中移动平均只用窗口内部、左右各 `SMOOTH//2` 根。窗口右端不够的地方会被截断，和实盘对「最后一根已收盘 K 线」做一次 `analyze()` 相同。
- 每一行的时间戳是窗口的最后一根，不是窗口中部。中部的点当时还看不到右侧的 K 线，不能当成当时的读数。
- 窗口里若缺一根（时间间隔不是这档周期），整窗丢弃。几何把每根 K 线的 dt 记为 1，跨缺口粘起来会算错。

## 数据不进 git

`data/ohlc/` 和 `data/backtest/` 在 `.gitignore` 里。多年的 K 线不要提交。拉一次就会在本地生成。

## 命令

仓库根目录，Python 3 标准库，无第三方包。

不访问网络（夹具 + 滚动、对账、假设表）：

```bash
python backtest_neff.py smoke
python -m unittest tests.test_backtest
```

`smoke` 应打印 `smoke ok`，以及一张小的假设表。单测覆盖：信号打在窗口末端、后一根 K 线不改前面的行、缺口被丢弃、OKX 翻页、对账容差、止损存根的高低点判定。

只看将要请求什么、不访问网络：

```bash
python backfill_ohlc.py --dry-run --days 90 --bars 5m,1H,4H
```

应打印每个周期的起止时间、大约多少页，以及最后一行 `dry-run: no request sent`。

回补、重放、对账、假设表：

```bash
python backfill_ohlc.py --inst BTC-USDT --bars 5m,1H,4H --days 90
python backtest_neff.py all --bars 5m,1H,4H
```

`--days` 是希望覆盖的信号天数，脚本会再往前多取 `WINDOW - 1` 根做暖机。4H 的暖机大约 10 天。仓库里的历史从 2026-09-17 左右开始，默认 90 天盖得住。只要其中一段可以用 `--since 2026-09-01`（不要和 `--days` 同时给）。

对账单独跑：

```bash
python backtest_neff.py parity --bars 5m,1H,4H
```

OHLC 没下全、但重叠的那些 candle 已经对齐时，可以加 `--allow-missing`：缺的时间戳不再导致失败，数值对不上仍然失败。

## 产物

| 路径 | 内容 |
|---|---|
| `data/ohlc/BTC-USDT_5m.csv`（以及 `1H`、`4H`） | `ts,open,high,low,close,vol`，仅 `confirm=1` |
| `data/backtest/BTC-USDT_5m_panel.csv` | 每根能算的 K 线一行：`candle_ts`、`n_eff`、`consistency`、`c_ratio`、`efficiency_ratio`、`state`、`sigma_per_bar` 等 |
| `data/backtest/parity.txt` | 对账摘要，最后一行 `PASS` 或 `FAIL` |
| `data/backtest/hypothesis.txt` | 给人看的表 |
| `data/backtest/hypothesis_range.csv` | 下一根的 \|Δclose\| 和 high−low |
| `data/backtest/hypothesis_stop.csv` | 下一根高低点是否碰到 ±k·σ |

面板里的 `candle_ts` 就是这根 K 线在 OKX 上的开盘时间（毫秒），和历史 CSV 的 `candle_ts` 是同一列。

## 对账怎么算过

按 `(instrument, bar, candle_ts)` 对齐。同一个 candle 在历史里被快照了多次时，用最新的 `generated_at`。历史文件自己打架会计入 `conflicts`，比较的仍是最新那条。

容差大约是各字段存储小数位的半步（`n_eff` 存两位，差 0.01 算失败）。收盘价不一致会直接记成不匹配。`PASS` 表示请求的每个周期都有重叠、没有超出容差的字段、也没有缺席的历史时间戳。

`c_ratio` 有一个旧口径。`migrate_history.py` 用已经四舍五入到 4 位的 `consistency`，去除未舍入的 `c_null(WINDOW)`。`analyze()` 两边都用未舍入的数，结果可以差大约 0.001。其余字段一致、且历史里的 `c_null` 就是当前窗口的基线时，对账接受这个旧值，并在 `parity.txt` 里写明有多少行。差到 0.01 仍然算失败。

缺席如果堆在文件开头，多半是暖机不够：把 `--days` 加大，或把 `--since` 再往前放，让最早那根历史 K 线前面还有完整的 `WINDOW` 根。

## 假设表怎么读

观察单位是一根已经收盘、并且前面有连续 `WINDOW` 根的 K 线。结果只看**下一根**，而且中间不能有缺口。这根 K 线自己的 high/low 属于已经走完的路径，不拿来当结果。

- `|Δclose| / σ` 和 `(high−low) / σ`。σ 是该窗口的 `sigma_per_bar`。
- 分组：`state`（`sharp` / `normal` / `circular` / `grinding`）、四舍五入后的 `n_eff < 5` 对 `≥ 5`（和 `label()` 的 sharp 分界同一刀）、样本内部的 N_eff 三分位。
- 对比行是两组中位数之比。接近 1 就是这批样本在这个代理上没有分开。`n < 30` 的组打了 `*`。
- 止损是存根：下一根的 high/low 是否碰到收盘价 ± k·σ（k = 0.5、1、1.5）。影线指碰到了但收盘回到带内。没有仓位，也不看更后面的 K 线。

现有实盘历史里几乎全是 `sharp` 和 `normal`，`circular` / `grinding` 没出现过。更长的离线样本里如果出现，表上会单独成组，不代表那就是可交易的状态。

## OKX 失败时

和 `neff.py` 一样按 `OKX_BASE` 依次试（默认 `https://www.okx.com,https://aws.okx.com,https://openapi.okx.com`）。美国 IP 上公共行情经常 403。失败会打印试过的域名并退出，不会写一个空的 CSV。

历史往回翻用 `/api/v5/market/history-candles`。OKX 的 `after` 表示「只要比这个 ts 更早的」，下一页的游标是本页最旧的 ts。最近约 1440 根再拿实盘用的 `/api/v5/market/candles` 盖一层；两边收盘价不一致时留下 candles 那条，并打印冲突根数。

分页之间的间隔是 `REQ_DELAY`（默认 0.15 秒）。`--limit` 最大 300。

网络不通时，用上面的 `smoke` 和 `unittest` 确认滚动与对账代码，不要把夹具当成行情样本。
