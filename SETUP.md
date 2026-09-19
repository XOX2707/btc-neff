# 部署步骤

文件已由 Claude 写入本目录。`.github` 是受保护路径，桥接工具无法直接写入，
所以工作流文件暂时叫 `MOVE-ME-neff-workflow.yml` 放在根目录，需要你手动归位。

## 在本目录打开 PowerShell，依次执行

```powershell
# 1. 把工作流放到正确位置
New-Item -ItemType Directory -Force -Path .github\workflows | Out-Null
Move-Item MOVE-ME-neff-workflow.yml .github\workflows\neff.yml -Force

# 2. 本地验证（零依赖，只用标准库）
python neff.py --selftest    # 合成数据，应看到平滑序列 N_eff≈11、加噪序列 N_eff≈3
python neff.py               # 真实抓取 OKX，写入 data/

# 3. 初始化并推送
git init -b main
git add -A
git commit -m "init: BTC N_eff smoothness tracker"
gh repo create btc-neff --private --source=. --push
```

最后一行需要 `gh` 已登录。没装 gh 就先在 GitHub 网页建空仓库，然后：

```powershell
git remote add origin https://github.com/<你的用户名>/btc-neff.git
git push -u origin main
```

## 推送后必做

GitHub 仓库页 → **Settings → Actions → General → Workflow permissions**
→ 选 **Read and write permissions** → Save。

不改这一项，机器人回写 `data/` 的提交会被拒绝，工作流会在最后一步失败。

## 验证

Actions 页签 → **BTC N_eff** → **Run workflow** 手动跑一次。
成功后仓库里会出现 `data/neff_latest.json` 和 `data/neff_history.csv`，
之后每 15 分钟自动更新。

## 如果第 2 步真实抓取失败

说明你本机网络到 OKX 不通（代理/区域限制）。这不影响 GitHub Actions ——
runner 在 GitHub 的网络里，与你本机无关。直接跳到第 3 步，
用 Actions 页的手动触发来验证真实链路即可。
