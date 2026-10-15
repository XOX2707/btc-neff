# BTC N_eff — 用多边形边数度量比特币走势的光滑度

把「三角形 → 八边形 → 圆」的趋势变成一个可计算的实时指标，全自动运行在 GitHub Actions 上，结果写回本仓库。

## 原理

正 N 边形（外切于圆）的周长与圆周之比：

    κ(N) = (N/π)·tan(π/N)  →  1   (N → ∞)

小角展开 κ ≈ 1 + π²/(3N²)，反解得**等效边数**：

    N_eff = π / √(3(κ−1))

其中 κ = 实际价格路径长度 ÷ 平滑基准路径长度。N_eff 越大，走势越接近圆弧；N_eff → 3 就是尖锐折线。

## 输出的四个量

| 字段 | 含义 |
|---|---|
| `n_eff` | 由 κ 反解的等效边数。**相对量**：以居中移动平均为基准 |
| `n_turn` | 2π / 平均\|转角\|。**绝对量**，不依赖基准，用于交叉验证 |
| `consistency` | \|Στ\| / Σ\|τ\|，转向一致性。圆弧 ≈ 1，锯齿 ≈ 0 |
| `efficiency_ratio` | Kaufman 效率比，净位移 / 总行程 |

`n_eff` 和 `n_turn` 数值不会相等——两者基准不同，看的是各自的**时间序列变化**，不是绝对值。自测中平滑正弦给出 n_eff=11.2 / C=1.00，同幅度加噪给出 n_eff=3.2 / C=0.01，区分度清晰。

**状态按随机游走分档。** 价格在这套归一化下和随机游走很接近：同窗口（60 根、平滑 5）的纯随机游走，N_eff 中位数约 4.1，99.9% 都落在 5 以下。所以分界不是拍的固定值，而是同窗口随机游走 N_eff 的分位数（`neff.state_cuts()`，固定种子模拟，每次运行结果相同），和 `c_ratio` 用 `c_null` 当基准是一个思路：

- `sharp` — N_eff < 随机 5% 分位（约 3.92），比随机游走折得更密
- `normal` — 5%–95% 分位（约 3.92–4.49），和随机游走分不开
- `smooth` — 95%–99.9% 分位（约 4.49–4.93），比 95% 的随机路径平滑
- `circular` — ≥ 99.9% 分位（约 4.93），随机游走千次里不到一次

具体分界写在 `neff_latest.json` 的 `state_cuts` 里，页面直接读。C 不再参与分档：一年的 5m 数据里 C 最大只有 0.11，和随机游走差不多，看 `c_ratio` 即可。

以前的固定分界（`sharp` N_eff < 5，`circular` / `grinding` 要 N_eff ≥ 12）在真实价格上到不了：一年 5m 的 N_eff 最大 7.7，没有噪声的正弦也只到 11 左右，结果 95% 以上的时间都是 `sharp`，后两档永远是 0。`neff.py` 每次运行都会把历史 CSV 里的 `state` 列按当前分界重写一遍（没变化就不动文件），所以历史、实时页和离线对账用的是同一套分档。

## 归一化

价格差分除以窗口内标准差，时间轴每根 K 线记 1。没有这一步，图表纵横比一变「转角」就变了，N_eff 毫无意义。

## 运行

推到 GitHub 后，`.github/workflows/neff.yml` 按 cron `0,30 * * * *` 自动跑（约每 30 分钟，每小时的 0 分和 30 分）。手动触发：Actions → BTC N_eff → Run workflow。

本地：

    python neff.py --selftest   # 合成数据验证
    python neff.py              # 真实抓取，写入 data/

零依赖，只用标准库。`migrate_history.py` 是一次性历史迁移脚本（按已存的 κ 重算 n_eff），不是主入口。

离线回补 K 线、滚动重放和与 `data/neff_history.csv` 的对账在 [BACKTEST.md](BACKTEST.md)。实时任务每跑完一次，`.github/workflows/backtest.yml` 就把那条链路滚动到最新 K 线并重新导出，[离线回测页](https://xox2707.github.io/btc-neff/backtest.html) 和实时页同步更新，打开就能看，不用再跑 Python。

## 输出文件

- `data/neff_latest.json` — 最新快照，含全部时间框架
- `data/neff_history.csv` — 追加式历史，每次运行每个时间框架一行

多尺度是本质：多边形趋近圆本身就是个尺度极限。N_eff 随时间框架的衰减曲线等价于分形维数 D = 2−H，比任何单一数值都有信息量。

## 配置

环境变量（在 `.github/workflows/neff.yml` 的 `env:` 里改）：

`INST_IDS` `OKX_BASE` `BARS` `WINDOW` `SMOOTH` `OUT_DIR`

`INST_IDS` 是逗号分隔的交易对（workflow 默认 `BTC-USDT`）。`INST_ID` 只作旧配置兼容：未设置 `INST_IDS` 时才会读取。

## 已知风险

- **GitHub 托管的 runner 在美国**。OKX 对美国 IP 有区域限制，公共行情接口通常可达，但若持续 403，脚本会记录错误并拒绝写入空结果。届时的选项：改用自托管 runner，或换一个数据源。多域名回退（`www` → `aws` → `openapi`）已内置。
- `0,30` 的 cron 是「不早于」，GitHub 在高峰期会延迟甚至跳过。这是尽力而为的调度，不是实时保证。
- 只使用已确认收盘的 K 线（OKX 第 9 个字段 `confirm == "1"`），未收盘的那根会被丢弃。

## 构建说明

指标构想由 [@XOX2707](https://github.com/XOX2707) 提出；数学推导、实现与调试由 Claude (Anthropic) 完成。
