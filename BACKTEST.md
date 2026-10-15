# 离线回测

实时脚本每次只向 OKX `/api/v5/market/candles` 要最近约 100 根已收盘 K 线。`data/neff_history.csv` 是这些读数的稀疏快照，没有原始 OHLC，不能当作回测样本。这里另做一条离线链路：回补历史 K 线，对每一根已收盘 K 线调用同一个 `analyze()`，再和已有快照对账，并给出第一张描述性的表。

它不回答「该不该买」。N_eff 是走完之后的几何描述，下面的表只比较下一根 K 线的绝对波动。

实时任务 `.github/workflows/neff.yml` 不跑这些脚本；`.github/workflows/backtest.yml` 每个 UTC 日刷新一次回测页（见下面「自动同步」）。

## 和实盘同一套数学

- κ、N_eff、C、c_ratio、ER、state、σ 全部来自 `neff.analyze`。回放没有第二套公式。state 的分界是 `neff.state_cuts()`：同窗口随机游走 N_eff 的分位数，种子固定，实盘和回放算出来一样。
- `WINDOW`（默认 60）和 `SMOOTH`（默认 5）跟实盘一样，从环境变量读。对账时必须和写历史时一致。
- 居中移动平均只用窗口内部、左右各 `SMOOTH//2` 根。窗口右端不够的地方会被截断，和实盘对「最后一根已收盘 K 线」做一次 `analyze()` 相同。
- 每一行的时间戳是窗口的最后一根，不是窗口中部。中部的点当时还看不到右侧的 K 线，不能当成当时的读数。
- 窗口里若缺一根（时间间隔不是这档周期），整窗丢弃。几何把每根 K 线的 dt 记为 1，跨缺口粘起来会算错。

## 数据不进 git

`data/ohlc/` 和 `data/backtest/` 在 `.gitignore` 里。多年的 K 线不要提交。拉一次就会在本地生成；自动同步时 `data/ohlc/` 放在 Actions 缓存里。

网页用的摘要在 `data/backtest_web/`，体积小，要提交。`.gitignore` 只排除上面两个目录；`data/backtest_web/` 是另一个路径，否定规则写在后面，避免以后加宽 `data/` 时把它一起藏掉。

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
python backfill_ohlc.py --dry-run --bars 5m,1H,4H
```

应打印每个周期的起止时间、大约多少页，以及最后一行 `dry-run: no request sent`。

回补、重放、对账、假设表：

```bash
python backfill_ohlc.py --inst BTC-USDT --bars 5m,1H,4H
python backtest_neff.py all --bars 5m,1H,4H
```

不写 `--days` 时，比 1H 更细的周期默认 **365** 天，1H 和更慢的周期默认 **730** 天。脚本还会再往前多取 `WINDOW - 1` 根做暖机（4H 大约 10 天）。

5m 的 365 天大约 350 页（`limit` 300，`REQ_DELAY` 0.15 秒，大约两三分钟）。OKX 的 `history-candles` 至少还能再往回三年，这不是接口封顶，6 个月也不是上限。要两年 5m 就显式写 `--days 730`（大约 700 页）。所有周期用同一个长度：`--days 365`。只要其中一段可以用 `--since 2026-01-01`（不要和 `--days` 同时给）。

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
| `data/backtest_web/summary.json` | 给网页的摘要：跨度、根数、状态计数、对账、假设表。要提交 |
| `data/backtest_web/hypothesis_range.csv` | 同上范围表的小副本，要提交 |
| `data/backtest_web/hypothesis_stop.csv` | 同上止损存根的小副本，要提交 |

面板里的 `candle_ts` 就是这根 K 线在 OKX 上的开盘时间（毫秒），和历史 CSV 的 `candle_ts` 是同一列。

## 网页

GitHub Pages：<https://xox2707.github.io/btc-neff/backtest.html>（仓库根目录的 `backtest.html`）。实时看板在 <https://xox2707.github.io/btc-neff/>，两页互相链着。

页面只读已提交的 `data/backtest_web/summary.json`，每 5 分钟重读一次。不读 `data/ohlc/`，也不读面板。页上的数字是最近一次导出，和下面「一次更长的样本」那张表同一套默认跨度；两者不同时以 `summary.json` 为准。

### 自动同步

`.github/workflows/backtest.yml` 仍由 `workflow_run` 触发，也就是实时工作流 `BTC N_eff` 每成功跑完一次就启动一次，因为实时任务是最准时的时钟（GitHub 自己的定时触发会晚几个小时）。但第一步会先看已提交的 `summary.json` 是哪天导出的：同一个 UTC 日里已经导出过，就跳过后面所有步骤，十几秒结束。所以实际每天只导出一次，在 UTC 0 点（北京时间 8 点）之后实时任务第一次跑完时。手动触发不受这个限制。一年的样本多半小时只动到小数点后第四位，每 30 分钟导出一次只是在制造提交和 Pages 部署。

要导出时，它会

1. 从 Actions 缓存恢复 `data/ohlc/`；
2. `python backfill_ohlc.py --incremental`：保留已有 K 线，只重新下载最后 `WINDOW` 根和更新的部分，并把早于默认跨度起点的行丢掉，跨度随时间往前滚；
3. `python backtest_neff.py replay`，再 `python export_backtest_web.py`（对账用实时任务刚提交的 `neff_history.csv`）；
4. 提交 `data/backtest_web/`。推送被拒（实时任务刚推过）就以远端为准重新导出，最多 3 次。

缓存每个 UTC 日存一份。第一次没有缓存时整段下载，大约 3–4 分钟；之后用前一天的缓存，每个周期只补一两页，整个任务一分钟左右，主要花在重放上。对账 `FAIL` 不会让任务失败，结论写进摘要，页面上显示。OKX 拒绝请求时任务失败、不提交，页面保留上一次的导出，副标题的「导出于」不再前进。手动触发：Actions → BTC N_eff backtest → Run workflow。

本地手动刷新也照样可以（`WINDOW` / `SMOOTH` 要和回放时一致）：

```bash
python backfill_ohlc.py --inst BTC-USDT --bars 5m,1H,4H
python backtest_neff.py all --bars 5m,1H,4H
python export_backtest_web.py
```

然后提交 `data/backtest_web/`。导出脚本读面板里的 `state` 和 `sigma_per_bar`，假设表重新调用 `neff_offline.build_hypothesis`，不另写公式。OHLC 不在旁边时，改读 `data/backtest/hypothesis_range.csv` 和 `hypothesis_stop.csv`。

没有本地产物、只想让页面说明「还没有样本」时：

```bash
python export_backtest_web.py --placeholder
```

这会写明是占位，不会填波动数字。占位不要和一次真实导出混着提交。

## 对账怎么算过

按 `(instrument, bar, candle_ts)` 对齐。同一个 candle 在历史里被快照了多次时，用最新的 `generated_at`。历史文件自己打架会计入 `conflicts`，比较的仍是最新那条。

容差大约是各字段存储小数位的半步（`n_eff` 存两位，差 0.01 算失败）。收盘价不一致会直接记成不匹配。`PASS` 表示请求的每个周期都有重叠、没有超出容差的字段、也没有缺席的历史时间戳。

`c_ratio` 有一个旧口径。`migrate_history.py` 用已经四舍五入到 4 位的 `consistency`，去除未舍入的 `c_null(WINDOW)`。`analyze()` 两边都用未舍入的数，结果可以差大约 0.001。其余字段一致、且历史里的 `c_null` 就是当前窗口的基线时，对账接受这个旧值，并在 `parity.txt` 里写明有多少行。差到 0.01 仍然算失败。

缺席如果堆在文件开头，多半是暖机不够：把 `--days` 加大，或把 `--since` 再往前放，让最早那根历史 K 线前面还有完整的 `WINDOW` 根。

## 假设表怎么读

观察单位是一根已经收盘、并且前面有连续 `WINDOW` 根的 K 线。结果只看**下一根**，而且中间不能有缺口。这根 K 线自己的 high/low 属于已经走完的路径，不拿来当结果。

- `|Δclose| / σ` 和 `(high−low) / σ`。σ 是该窗口的 `sigma_per_bar`。
- 分组：`state`（`sharp` / `normal` / `smooth` / `circular`）、四舍五入后的 N_eff 低于对高于随机 95% 分位（切在 `label()` 的 normal / smooth 分界，即 sharp+normal 对 smooth+circular）、样本内部的 N_eff 三分位。
- 对比行：normal 对 smooth、低于对高于随机 95% 分位、三分位低对高。左边都是更不平滑的一组。
- 对比行是两组中位数之比。接近 1 就是这批样本在这个代理上没有分开。`n < 30` 的组打了 `*`。
- 止损是存根：下一根的 high/low 是否碰到收盘价 ± k·σ（k = 0.5、1、1.5）。影线指碰到了但收盘回到带内。没有仓位，也不看更后面的 K 线。

状态是相对随机游走的描述。`smooth`、`circular` 只说明这一窗比随机路径平滑，不代表那就是可交易的状态。

## 状态改按随机游走分档（2026-10-15）

旧分界（`sharp` N_eff < 5、`circular` / `grinding` 要 N_eff ≥ 12）在下面那次样本里，`circular` / `grinding` 一次都没出现，95% 以上是 `sharp`。原因是 N_eff 在这套归一化下根本到不了 12：一年 5m 最大 7.7；没有噪声的正弦（C = 1）也只有 11 左右。同窗口随机游走的 N_eff 中位数 4.14、99.9% 分位 4.95，BTC 的中位数 4.23，和随机游走很近。

现在的分界是随机游走的 5% / 95% / 99.9% 分位，窗口 60、平滑 5 时是 3.92 / 4.49 / 4.93。同一份默认跨度重放：

| 周期 | sharp | normal | smooth | circular |
|---|---|---|---|---|
| 5m | 1648（1.6%） | 85732（81.6%） | 14682（14.0%） | 3057（2.9%） |
| 1H | 89（0.5%） | 12469（71.2%） | 3820（21.8%） | 1141（6.5%） |
| 4H | 2（0.0%） | 3322（75.9%） | 835（19.1%） | 220（5.0%） |

历史按新分界重写 `state` 后对账仍是 `PASS`（5m 138/138、1H 132/132、4H 121/121）。

下一根 `|Δclose|/σ` 中位数之比（左边是更不平滑的一组）：normal 对 smooth 是 5m 1.04、1H 1.10、4H 1.01；低于对高于随机 95% 分位是 5m 1.07、1H 1.13、4H 1.00。比随机平滑的窗口，下一根的波动只略小一点，4H 上没有分开。这仍是描述，不是成本优势。

## 转移事件实验（2026-10-15）

`experiment_transitions.py` 不再比下一根的波动宽窄，改问方向：状态**变化**（`normal→smooth`、`smooth→circular` 等）之后，价格是否沿窗口原来的方向继续走，而且这件事是窗口自己的过去涨跌和波动说明不了的。

```bash
python experiment_transitions.py --bars 5m,1H,4H --horizons 1,3,12,48
```

读 `data/backtest/` 的面板和 `data/ohlc/` 的 K 线，写 `data/backtest/transitions.txt` 和 `transitions.csv`，都不提交。

- **事件**：这一行的 state 和上一根不同（必须连续）。分界就是实盘的 `label()`，这里不调任何参数。
- **方向**：N_eff 本身没有方向，唯一能检验的方向是「延续」。d = 窗口净位移的符号。`cont` = d ×（H 根后收盘 − 当前收盘）/（σ·√H）。另记命中率、H 根内最大不利变动（MAE），以及 ±σ·√H 哪一边先被碰到。
- **基准**：只在样本内拟合。`edge|r|` 是事件减去同一「窗口 |净位移|/σ」五分位里所有 K 线的平均 `cont`，对应「只用过去 |收益|」；`edgeVol` 是减去同一「σ/价格」五分位的平均，对应「只用过去波动」。
- **样本外**：按时间切，前 70% 是样本内，后 30% 是样本外，两段并排打印，不在任何一段上挑选。事件类型按样本内次数（≥ 30）固定下来，不看结果。
- **重叠**：同类事件之间至少隔 H 根，才进入均值和 t 值。
- **成本**：同时给出以 bps 计的平均延续收益，旁边写着往返成本（默认 10 bps）。

默认跨度上的结果（5m 365 天，1H、4H 730 天）：

- 一共 72 组「事件类型 × 周期 × 持有期」。样本内和样本外 edge 同号的只有 31 组（43%），和抛硬币差不多。
- 没有一组在两段里都同号且 |t| ≥ 1.5。看起来最强的几组都是在样本外翻了号，例如 5m `normal→smooth`、H=1：样本内 edge −0.095（t −2.1），样本外 +0.179（t +2.3）；1H `smooth→normal`、H=3：样本内 +0.005，样本外 −0.331（t −2.8）。
- 5m 上事件后的平均延续收益在 ±3 bps 以内，低于 10 bps 的往返成本。1H、4H 的 bps 更大，但样本少，而且两段之间方向不稳定。
- 4H 的转移事件在样本外只有 7–27 个，不能下结论。

结论：在现在的定义下，状态转移不带方向信息，延续的强弱也超不出「只用过去 |收益|」和「只用过去波动」这两个基准。要继续追，应换特征（连续的 N_eff、C、ER 一起用，或多周期同时偏离），而不是继续调分界。

## 多周期共振与连续特征（2026-10-15）

`experiment_features.py` 做清单里的第 3、4 条，切分、基准和事件间隔的规则和上面一样。

```bash
python experiment_features.py --base 5m --others 1H,4H
python experiment_features.py --base 1H --others 4H
```

结果写到 `data/backtest/features_<base>.txt`，不提交。

**A：多周期共振。** 在每一根基准周期的 K 线上，取其他周期最近一根**已经收盘**的面板行（1H 那一行要等这一小时走完才算），不会用到未来数据。分组：
- `agree`：各周期窗口的涨跌方向一致
- `agree+smooth`：方向一致，且各周期都是 smooth 或 circular
- `agree+2smooth`：方向一致，且至少两个周期是 smooth 或 circular（三个周期时才有）
- `agree+rough`：方向一致，且各周期都是 sharp 或 normal
- `all smooth`：各周期都是 smooth 或 circular，不管方向

结果看的是沿基准窗口方向的延续。`vsAgree` 是减去样本内单纯 `agree` 的均值，也就是「只靠多周期方向一致」能得到的部分。几何要算有用，得是 `agree+smooth` 赢过 `agree`。

**B：连续特征进模型。** 两个嵌套模型，每个持有期各拟合一次：
- M0：各周期过去的有符号收益 z、|z|、σ/价格
- M1：M0 再加上各周期的 n_eff、c_ratio、ER、n_turn，以及它们各自乘以该周期涨跌方向的交互项

目标一是下一段收益的符号，用带小 ridge 的逻辑回归；目标二是 |移动|/(σ·√H)，用最小二乘。特征按样本内标准化；训练每 4 行取 1 行，相邻窗口有 59/60 根重叠，这样损失的信息不多。样本外报告 log loss、准确率、AUC、R²，以及逐行 M1−M0 的损失差（行间隔 H，算 t）。交易代理：只在 |p−0.5| 超过样本内 90% 分位时按 p 的方向下单，每 H 根最多一笔、互不重叠，报告每笔的 bps 和 t。

默认跨度上的结果：

- **A，5m/1H/4H**：`agree+smooth` 只占 0.4%–1.2% 的 K 线。样本内为负或接近 0，样本外为正，但 n 小（23–375），t ≤ 1.5，又是两段之间翻号。`agree+2smooth` 在两段里都赢不了 `agree`。`all smooth` 在 H=12 样本外 t 2.0，样本内 −0.2。
- **A，1H/4H**：`agree+smooth` 在 H=3、H=12 两段都为正（样本内 t 1.2 / 0.8，样本外 0.4 / 0.8），方向对了但远不显著，样本外只有 30–120 个。`agree+rough` 在样本外 H=12、H=48 是 t −2.0 和 −2.2，样本内是 +0.0 和 +0.1，又是只在一段里出现。
- **B，5m**：几何没帮上忙。H=1 时 M1 的 log loss 反而更差（M1−M0 = +0.00088，t 3.9）；H=3、H=12 时 M1 预测幅度的平方误差也更差（t 2.3、1.8）。AUC 都在 0.51–0.55。交易代理在 −2.3 到 +7.5 bps 之间，没有一个过 10 bps 成本且 t ≥ 2。
- **B，1H**：只有 H=12 的 M1 交易代理看起来不错，114 笔、+28.1 bps/笔、t 2.3。但它不稳：把切分改成 60/40 是 +9.1 bps（t 0.6），改成 80/20 是 +6.7 bps（t 0.3），而且 82%–90% 是做多。同一个 H 上 M1 的 log loss 只略好一点（t −1.0），平方误差反而更差。这是 16 个交易代理里的一个，更像多重比较里的偶然，加上样本外偏多头的行情。

结论：多周期一致和连续几何特征，在样本外都没有稳定地超过「只用过去收益和波动」的基准。和转移事件实验合在一起看，N_eff 这套几何量描述的是走势形状，对后续方向和幅度没有可交易的增量。

## 执行环境实验（2026-10-15）

`POSTMORTEM.md` 里有一句不需要预测方向的说法：N_eff 低（走势锯齿）时反转多，吃单反复付点差和滑点，挂单反而会被这些反转喂到成交。`experiment_execution.py` 用现有 K 线检验这一句。

```bash
python experiment_execution.py --bars 5m,1H,4H --ks 0.5,1 --horizons 1,3,12
```

结果写到 `data/backtest/execution.txt`，不提交。

- **挂单**：每根 K 线收盘时，在收盘价下方 k·σ 挂买单、上方 k·σ 挂卖单，有效 `--valid` 根（默认 1）。之后某根的最低价（最高价）碰到挂单价就算成交；如果那根开盘就已经越过挂单价，按开盘价成交。`--through` 要求价格越过挂单价若干 σ 才算成交，粗略地模拟排队。
- **成交后的盈亏（markout）**：从成交价到成交后第 H 根收盘的变动，以 bps 计，对挂单方有利为正。
- **基准**：先减去样本内同一「σ/价格」五分位（或同一「窗口 |收益|」五分位）所有成交的平均 markout，得到残差；再比较 rough（sharp+normal）减 smooth+（smooth+circular）的残差均值，用 Welch t，成交先按单边间隔 H 根筛过。
- **判定（事先定好）**：波动残差的差值在样本内外同号、两段 |t| 都 ≥ 2、样本外差值至少等于吃单和挂单的费率差（`--fee-gap`，默认单边 3 bps），才算 PASS，否则 FAIL。
- **局限**：K 线「碰到就成交」偏乐观，没有排队、部分成交和点差。所以 FAIL 有说服力，PASS 还得用逐笔成交或盘口数据再验一次。

默认跨度上：**18 个判定全部 FAIL**（3 个周期 × k 取 0.5、1 × H 取 1、3、12）。

- **成交后的盈亏和状态无关**：5m 的被动成交平均 markout 约 −0.7 到 −1.2 bps，这是被动成交的逆向选择；rough 和 smooth+ 的残差差值在 ±0.5 bps 以内，|t| ≤ 1。把 `--through` 设成 0.1σ，结论不变。
- **1H、4H**：差值时正时负，在样本内外之间翻号，|t| ≤ 1.5。
- **唯一稳定的一点是成交率**：5m、k=0.5 时，rough 的成交率比 smooth+ 高约 1.5 个百分点（样本内 0.496 对 0.477，样本外 0.481 对 0.466）。但多成交的这些单子 markout 并不更好，换不成钱。

结论：在 K 线精度下，「锯齿时段挂单更划算」不成立。

## 汇总：预测力检验（2026-10-15）

四组实验（转移事件、多周期共振、连续特征模型、执行环境），规则一致：按时间 70/30 切样本内外、基准只在样本内拟合、事件至少间隔 H 根才算 t、对照成本。没有一组在样本外稳定地超过「只用过去收益和波动」的基准。

转移实验和特征实验的输出里各有一列 `net`，即扣掉 `--cost-bps`（默认往返 10 bps）之后的 bps。5m 上全部为负。

N_eff 这套几何量适合描述当前走势的形状（实时页和回测页就是做这件事），不适合当交易或执行信号。要再往下追，得换数据（逐笔成交、盘口）或换问题，而不是在同一批 K 线上换参数。

## 一次更长的样本（2026-10-15，旧分界）

默认跨度，BTC-USDT，缺口 0，两个行情接口的收盘价没有冲突。对账 `PASS`：5m 134/134、1H 130/130、4H 120/120。其中 10 行只差迁移脚本的 `c_ratio` 口径。

| 周期 | 信号跨度 | K 线根数 | sharp | normal | circular / grinding |
|---|---|---|---|---|---|
| 5m | 365 天（2025-10-15 .. 2026-10-15） | 105178 | 102687 | 2432 | 无 |
| 1H | 730 天（2024-10-15 .. 2026-10-15） | 17578 | 16616 | 903 | 无 |
| 4H | 730 天（2024-10-15 .. 2026-10-15） | 4438 | 4215 | 164 | 无 |

和之前约 90 天的样本比，sharp 对 normal 的下一根 `|Δclose|/σ` 中位数之比：5m 从 1.23 到 1.21，1H 从 1.26 到 1.15，4H 从 0.78 到 0.90。5m、1H 的方向没翻，差距略收窄。4H 上「normal 更宽」减弱，`(high−low)/σ` 之比收到 1.01，k=1 的止损触碰率两边都约 0.46。这仍是描述，不是成本优势。

同一天按默认跨度又导出了一份到 `data/backtest_web/`，状态计数与上表相同。实盘历史在那之后又多了一轮快照，所以网页上的对账是 5m 135/135、1H 131/131、4H 121/121，仍是 `PASS`，旧 `c_ratio` 口径仍是 10 行。sharp 对 normal 的下一根 `|Δclose|/σ` 之比是 5m 1.205、1H 1.150、4H 0.899。以后再导出，以页面为准。

## OKX 失败时

和 `neff.py` 一样按 `OKX_BASE` 依次试（默认 `https://www.okx.com,https://aws.okx.com,https://openapi.okx.com`）。美国 IP 上公共行情经常 403。失败会打印试过的域名并退出，不会写一个空的 CSV。

历史往回翻用 `/api/v5/market/history-candles`。OKX 的 `after` 表示「只要比这个 ts 更早的」，下一页的游标是本页最旧的 ts。最近约 1440 根再拿实盘用的 `/api/v5/market/candles` 盖一层；两边收盘价不一致时留下 candles 那条，并打印冲突根数。

分页之间的间隔是 `REQ_DELAY`（默认 0.15 秒）。`--limit` 最大 300。

网络不通时，用上面的 `smoke` 和 `unittest` 确认滚动与对账代码，不要把夹具当成行情样本。
