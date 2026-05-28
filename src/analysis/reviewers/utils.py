"""code_review.py — PR 代码审查 Agent 模块

自动审查代码变更，检测安全、性能、代码质量问题，输出质量报告。

模块结构:
  - SecurityReviewer    安全审查（SQL注入、命令注入、密钥泄露、XSS）
  - PerformanceReviewer 性能审查（N+1查询、sync I/O阻塞、内存泄漏）
  - QualityReviewer     代码质量审查（未用import、过深嵌套、硬编码值）
  - PRReviewer          综合 PR 审查（调用三个审查器，输出总评分和审核结论）

设计理由:
  - 纯 Python 标准库，无外部依赖，开箱即用
  - 正则 + AST 双重检测：正则捕获字面量，AST 捕获结构问题
  - 按严重级别分级（critical/high/medium/low），下游可灵活决策

面试官可能问:
  - 为什么不用 flake8/pylint？答：它们只检查质量不检查安全；此模块兼做安全和性能
  - 覆盖 JavaScript 吗？答：XSS 检测部分覆盖 JS，完整 JS 支持需要补充
  - 误报率怎么控制？答：白名单 + AST 分析减少正则误报
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import re
from pathlib import Path




def check_python_file(path: str) -> dict:
    """单个 Python 文件的完整审查

    Args:
        path: Python 文件路径

    Returns:
        dict: {
            "file": str,
            "total_issues": int,
            "issues": [各项问题],
            "overall_score": int,
        }
    """
    filepath = Path(path)
    if not filepath.exists():
        return {"file": path, "error": "文件不存在", "issues": [], "total_issues": 0}

    try:
        code = filepath.read_text(encoding="utf-8")
    except Exception as e:
        return {"file": path, "error": str(e), "issues": [], "total_issues": 0}

    reviewer = PRReviewer()
    result = reviewer.review_pr(code, [path])

    return {
        "file": path,
        "total_issues": len(result["security_issues"]) + len(result["performance_issues"]) + len(result["quality_issues"]),
        "security_issues": result["security_issues"],
        "performance_issues": result["performance_issues"],
        "quality_issues": result["quality_issues"],
        "overall_score": result["overall_score"],
    }


def review_project(path: str) -> dict:
    """对整个项目目录进行全面的代码审查

    Args:
        path: 项目根目录路径

    Returns:
        dict: {
            "project": str,
            "files_checked": int,
            "total_issues": int,
            "file_reports": [每个文件的审查结果],
            "overall_score": int,
        }
    """
    root = Path(path)
    if not root.is_dir():
        return {"project": path, "error": "目录不存在"}

    py_files = list(root.rglob("*.py"))
    py_files = [f for f in py_files if "__pycache__" not in str(f) and not f.name.startswith("_")]

    file_reports = []
    total_issues = 0
    for f in py_files:
        report = check_python_file(str(f))
        file_reports.append(report)
        total_issues += report.get("total_issues", 0)

    scores = [r.get("overall_score", 100) for r in file_reports if "overall_score" in r]
    avg_score = sum(scores) // len(scores) if scores else 100

    return {
        "project": path,
        "files_checked": len(py_files),
        "total_issues": total_issues,
        "file_reports": file_reports,
        "overall_score": avg_score,
    }




def handle_github_webhook(payload: dict) -> dict:
    """处理 GitHub Webhook 请求

    Args:
        payload: GitHub Webhook 的 JSON payload（pull_request 事件）

    Returns:
        dict: 审查结果，包含 review_comment

    Why:
        - 可以在 FastAPI 的路由中调用此函数
        - 返回的 review_comment 可直接通过 GitHub API 提交到 PR
    """
    action = payload.get("action", "")
    if action not in ("opened", "synchronize"):
        return {"skipped": True, "reason": f"action={action}，不需要审查"}

    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "")
    pr_body = pr.get("body", "")
    pr_diff_url = pr.get("diff_url", "")


    diff_text = f"PR #{pr_number}: {pr_title}\n\n{pr_body}"
    changed_files = [f.get("filename", "") for f in payload.get("pull_request", {}).get("files", [])]

    reviewer = PRReviewer()
    result = reviewer.review_pr(diff_text, changed_files)

    result["pr_number"] = pr_number
    result["pr_title"] = pr_title
    result["review_comment"] = reviewer.generate_comment(
        result.get("security_issues", [])
        + result.get("performance_issues", [])
        + result.get("quality_issues", [])
    )

    return result


if __name__ == "__main__":
    test_code = """import os
import sys
def get_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    cursor.execute(query)
    return cursor.fetchone()

def run_cmd(cmd):
    os.system("ping " + cmd)

API_KEY = "sk-1234567890abcdef1234567890abcdef"
"""
    reviewer = PRReviewer()
    result = reviewer.review_pr(test_code, ["test.py"])
    print(f"评分: {result['overall_score']}/100")
    print(f"结论: {result['verdict']}")
    print(f"发现问题: {len(result['security_issues'])} 安全 + {len(result['performance_issues'])} 性能 + {len(result['quality_issues'])} 质量")
    for i in result["security_issues"]:
        print(f"  [{i['severity']}] L{i['line']} {i['type']}: {i['description'][:50]}")
    print(f"\n{result['comments'][:500]}")
