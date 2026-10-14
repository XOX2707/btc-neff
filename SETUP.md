# 部署步骤

工作流已经在仓库里：`.github/workflows/neff.yml`。

调度是 cron `0,30 * * * *`（约每 30 分钟，每小时的 0 分和 30 分），也可以在 Actions 页手动触发。

## 本地验证

在本目录打开终端（零依赖，只用标准库）：

```powershell
python neff.py --selftest    # 合成数据，应看到平滑序列 N_eff≈11、加噪序列 N_eff≈3
python neff.py               # 真实抓取 OKX，写入 data/
```

## 推送后必做

GitHub 仓库页 → **Settings → Actions → General → Workflow permissions**
→ 选 **Read and write permissions** → Save。

不改这一项，机器人回写 `data/` 的提交会被拒绝，工作流会在最后一步失败。

## 验证

Actions 页签 → **BTC N_eff** → **Run workflow** 手动跑一次。
成功后仓库里会出现 `data/neff_latest.json` 和 `data/neff_history.csv`，
之后按 cron `0,30 * * * *` 自动更新（约每 30 分钟）。

## 如果真实抓取失败

说明你本机网络到 OKX 不通（代理/区域限制）。这不影响 GitHub Actions ——
runner 在 GitHub 的网络里，与你本机无关。
用 Actions 页的手动触发来验证真实链路即可。
