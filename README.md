# A股短线自动化交易系统

本地git仓库（/home/ubuntu/.hermes/scripts/），初始快照 2026-09-01。

## 目录结构

| 路径 | 内容 |
|------|------|
| *.py 根目录 | 系统脚本栈（33个）：short_term/stock_pool/decision_manager/position_manager/paper_trader/evening_analysis/review/rebound_pool/bottom_scan/etf_t_engine 等 |
| *.json *.csv | 交易与运行数据（见下） |
| cron_rules/ | 定时任务规则快照：jobs.json（7个cron任务完整定义含prompt）+ TASKS_SUMMARY.md |
| tests/ | 单测：test_decision_manager / test_stock_pool_v12 / test_pool_gate_v132 / test_stock_pool_ai_retry |
| archive/ | 旧版脚本归档（etf_v3/quant_engine/stock_daily 等） |
| *.md | 设计文档：trading_system_design / stock_pool_design_v1/v2 / T模块设计与分析流程 |

## 交易数据（实时更新，每次提交都会变化）

- positions.json — 持仓单一数据源（etf+stock）
- trade_log.csv — 实盘成交记录
- signal_log.csv — 分析信号日志（15列，含状态机动作）
- decision_state.json / decision_history.json — V3.0决策状态机持久状态
- watchlist.json — AI裁决次日监测名单
- stock_pool.json — 每日股票池（core/watch）
- paper_positions.json — 虚拟账户（paper_trader 20万模拟盘）

## 模拟交易数据

- paper_trader.py + paper_positions.json — 虚拟回放，每晚18:20增量更新signal_log状态字段
- t_state.json / t_trade_log.csv — ETF做T引擎（etf_t_engine.py）状态与成交

## 敏感文件（git已排除，勿提交）

- .qq_token_cache.json — QQ机器人access token缓存
- .env — 密钥（QQ_CLIENT_SECRET等，位于 ~/.hermes/.env）
- __pycache__/

## 常用命令

```bash
git add -A && git commit -m "快照 $(date +%Y%m%d_%H%M)"
# 查看状态机：python3 decision_manager.py --states
```

## 定时报告

- 股票池全量评分、WATCH名单、指标明细和AI裁决过程只保存在后台数据与日志中。
- `stock_pool_full.py`成功时只由最后一步推送一条用户摘要，不再分别推送股票池日报和AI裁决。
- 用户摘要最多1200字、最多3个可执行动作；无操作候选不逐只展开。
- 日内后续摘要读取上一轮结果，只报告新增、取消、升级、降级和价格失效。
- AI不可用时不发送全量原始数据，只报告本轮无可验证操作。
