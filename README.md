# XAUStudy 分析档案

这个目录保存黄金分析页面，并部署到 GitHub + Vercel。

- `index.html`: 网站总首页，分流到 Codex 分析和 Gemini 分析。
- `reports/index.html`: Codex 分析历史记录入口。
- `reports/YYYY-MM-DD.html`: Codex 每日分析快照。
- `gemini/reports/index.html`: Gemini 分析历史记录入口。
- `gemini/reports/*.html`: Gemini HTML 分析快照，来自本地 `gemini_xau` 历史目录。

以后生成 Codex 新分析时，推荐流程：

1. 先把当天页面保存为 `reports/YYYY-MM-DD.html`。
2. 在 `reports/index.html` 加一条历史记录。
3. 用 git commit 保存一次版本。
4. 推送 GitHub，让 Vercel 自动部署。

以后导入 Gemini 新分析时，推荐流程：

1. 把 Gemini HTML 保存到 `gemini/reports/`。
2. 在 `gemini/reports/index.html` 加一条历史记录。
3. 用 git commit 保存一次版本。
4. 推送 GitHub，让 Vercel 自动部署。
