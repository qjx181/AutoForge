#!/usr/bin/env python3
"""临时诊断脚本 — 运行委托失败模式分析。"""
import sys
sys.path.insert(0, "/mnt/f/项目三：多Agent")
from delegate_optimizer import diagnose_failures, write_diagnosis_to_log

d = diagnose_failures()
print("Errors:", d.get("error", "none"))
print("Total rounds:", d.get("total_rounds"))
print("Overall success rate:", d.get("overall_success_rate"))
print("Delegated rounds:", d.get("delegated_rounds"))
print("Delegate success rate:", d.get("delegate_success_rate"))
print("Failure patterns:", d.get("failure_patterns"))
print("Trend:", d.get("trend"))

if "error" not in d:
    ok = write_diagnosis_to_log(d)
    print(f"Write to log: {ok}")
