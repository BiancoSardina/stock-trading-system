#!/usr/bin/env python3
"""手动·盘后技术分析（裁决后触发）。cron 入口固定 slot=close，且默认强校验“本轮裁决已回写”。

放宽（裁决未回写也照跑）：python3 analysis_close.py --no-require-adjudication
"""
import sys

from analysis_after_decision import main

if __name__ == "__main__":
    sys.exit(main("close", require_adjudication_default=True))
