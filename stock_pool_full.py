#!/usr/bin/env python3
"""午盘/尾盘股票池：预算内生成完整批次，再归档上传；失败独立通知。"""
import sys
import time
from pool_pipeline import main

if __name__ == "__main__":
    sys.exit(main("午盘股票池" if time.localtime().tm_hour < 13 else "尾盘股票池"))
