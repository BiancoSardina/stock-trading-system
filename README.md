# A股短线自动化交易系统

本地git仓库（/home/ubuntu/.hermes/scripts/），初始快照 2026-09-01。

## 目录结构

| 路径 | 内容 |
|------|------|
| *.py 根目录 | 系统脚本栈（33个）：short_term/stock_pool/decision_manager/position_manager/paper_trader/evening_analysis/review/rebound_pool/bottom_scan/etf_t_engine 等 |
| *.json *.csv | 交易与运行数据（见下） |
| cron_rules/ | 定时任务规则快照：jobs.json（7个cron任务完整定义含prompt）+ TASKS_SUMMARY.md |
| tests/ | 单测：决策状态机、股票池、数据闸门、外部裁决数据包等 |
| archive/ | 旧版脚本归档（etf_v3/quant_engine/stock_daily 等） |
| *.md | 设计文档：trading_system_design / stock_pool_design_v1/v2 / T模块设计与分析流程 |

## 交易数据（实时更新，每次提交都会变化）

- positions.json — 持仓单一数据源（etf+stock）
- trade_log.csv — 实盘成交记录
- signal_log.csv — 分析信号日志（15列，含状态机动作）
- decision_state.json / decision_history.json — V3.0决策状态机持久状态
- watchlist.json — 手工维护的次日监测名单
- stock_pool.json — 每日股票池（core/watch）
- decision_bundle_latest.json — 上传给外部 AI 的实时裁决数据包（运行数据，不提交）
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

## 外部 AI 裁决数据包

- `stock_pool_full.py` 和 `stock_pool_evening.py` 只运行行情获取、确定性分析和数据包生成；不会调用本地大模型、写入 AI 监测名单或发送交易结论。
- 运行 `python decision_bundle.py --run-analysis` 会生成 `decision_bundle_latest.json`。将该文件上传到对话，由外部 AI 完成裁决。
- 数据包包含市场状态、股票池、真实持仓、已有观察名单和原始 Python 分析；生成后仅 5 分钟有效，过期必须重新采集。
