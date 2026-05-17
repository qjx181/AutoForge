#!/usr/bin/env python3
"""验证 swarm_metrics.py 的语法和导入"""
import ast
import sys

# 1) AST parse
with open('swarm_metrics.py') as f:
    ast.parse(f.read())
print("✅ AST parse OK")

# 2) Import
sys.path.insert(0, '.')
from swarm_metrics import SwarmMetrics
print("✅ Import OK")

# 3) Basic functionality test
m = SwarmMetrics()
m.start_round(round_num=1)
m.record_task(agent="agent-1", status="completed", duration_sec=120.0, task_name="实现模块")
m.record_issue(severity="error", category="logic_error", module="test", message="测试问题")
m.end_round()

report_text = m.generate_report(fmt="text")
print("✅ Text report generated")
print(report_text[:200])

report_json = m.generate_report(fmt="json")
print("✅ JSON report generated")

d = m.to_dict()
assert "timer" in d
assert "tasks" in d
assert "issues" in d
print("✅ to_dict OK")

# 4) Save & load
save_path = "tmp_agent/metrics/test_round.json"
m.save(save_path)
print(f"✅ Save OK: {save_path}")

m2 = SwarmMetrics.load(save_path)
assert m2 is not None
assert m2.timer.rounds[0]["round_num"] == 1
assert m2.tasks.tasks[0]["status"] == "completed"
assert m2.issues.issues[0]["severity"] == "error"
print("✅ Load & verify OK")

# 5) Pass rate
assert m.tasks.pass_rate() == 1.0
print("✅ Pass rate OK")

# 6) Issue frequency
freq = m.issues.frequency_by_severity()
assert freq["error"] == 1
assert freq.get("critical", 0) == 0
print("✅ Issue frequency OK")

# 7) Edge cases
m3 = SwarmMetrics()
assert m3.tasks.pass_rate() is None
print("✅ Empty pass rate is None OK")

print("\n🎉 All verification passed!")
