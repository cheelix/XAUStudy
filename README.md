# 黄金10指标看板

这个目录保存黄金 10 指标分析页面。

- `index.html`: 当前最新分析页面。
- `reports/index.html`: 历史记录入口。
- `reports/YYYY-MM-DD.html`: 每日分析快照。

以后生成新分析时，推荐流程：

1. 先把当天页面保存为 `reports/YYYY-MM-DD.html`。
2. 更新 `index.html` 为最新页面。
3. 在 `reports/index.html` 加一条历史记录。
4. 用 git commit 保存一次版本。

