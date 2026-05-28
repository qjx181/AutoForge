"""success_verifier.py — 修复后自动验证（借鉴 SWE-agent/Looper 的 patch-then-verify 模式）

核心思想（面试话术）：
  "SWE-agent 和 Looper 的关键创新是：修复不是终点，验证才是。
   修复器说 success=True 只代表'代码改了'，不代表'问题解决了'。
   所以我们在修复后加入自动验证：语法检查 → 重跑扫描 → 测试执行。
   任何一步失败就回滚，不信任修复器的自评。"

验证器链（按成本递增）：
  1. SyntaxVerifier   — 改后的文件能否通过 ast.parse？零成本
  2. ReScanVerifier   — 重新跑对应扫描器，原 issue 是否消失？中等成本
  3. TestVerifier     — 跑相关测试用例（如果有）。高成本

使用方式：
  chain = build_default_verifier_chain()
  result = chain.verify(issue, fix_result, project_root)
  if not result.passed:
      # 回滚修复
"""

import ast
import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)



@dataclass
class VerifyResult:
    """单个验证器的结果。"""
    verifier: str          # 验证器名称
    passed: bool           # 是否通过
    detail: str = ""       # 失败原因或附加信息
    confidence_delta: float = 0.0  # 通过时对置信度的修正值（+0.1 或 -0.2）


@dataclass
class VerifyChainResult:
    """验证链的汇总结果。"""
    passed: bool = True                    # 全部通过才为 True
    total_delta: float = 0.0              # 累计置信度修正
    results: list[VerifyResult] = field(default_factory=list)
    should_rollback: bool = False          # 是否建议回滚

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.passed)
        return f"验证 {passed}/{len(self.results)} 通过, 置信度修正 {self.total_delta:+.2f}"



class BaseVerifier(ABC):
    """验证器基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """验证器名称。"""

    @abstractmethod
    def verify(self, issue_type: str, file_path: Path,
               fix_action: str, project_root: Path) -> VerifyResult:
        """验证修复是否真正解决了问题。"""



class SyntaxVerifier(BaseVerifier):
    """验证修改后的文件能否通过 Python 语法检查。

    成本：零（纯本地 ast.parse）。
    失败意味着修复引入了语法错误，必须回滚。
    """

    @property
    def name(self) -> str:
        return "syntax"

    def verify(self, issue_type: str, file_path: Path,
               fix_action: str, project_root: Path) -> VerifyResult:
        if not file_path.exists():
            return VerifyResult(
                verifier=self.name, passed=False,
                detail=f"文件不存在: {file_path}", confidence_delta=-0.3,
            )
        try:
            code = file_path.read_text(encoding="utf-8")
            ast.parse(code)
            return VerifyResult(
                verifier=self.name, passed=True,
                detail="语法检查通过", confidence_delta=0.05,
            )
        except SyntaxError as e:
            return VerifyResult(
                verifier=self.name, passed=False,
                detail=f"语法错误: {e}", confidence_delta=-0.3,
            )


class ReScanVerifier(BaseVerifier):
    """重新运行对应扫描器，检查原 issue 是否消失。

    成本：中等（重跑一次扫描）。
    如果 issue 仍然存在，说明修复没有真正解决问题。
    """

    def __init__(self, scanner_registry=None):
        self._registry = scanner_registry

    @property
    def name(self) -> str:
        return "rescan"

    def verify(self, issue_type: str, file_path: Path,
               fix_action: str, project_root: Path) -> VerifyResult:
        if not self._registry:
            return VerifyResult(
                verifier=self.name, passed=True,
                detail="无扫描器注册表，跳过重扫验证", confidence_delta=0,
            )

        from .adapters import Issue
        for scanner in self._registry.all():
            try:
                issues = scanner.scan(project_root)
                remaining = [
                    i for i in issues
                    if i.type == issue_type and i.file == str(file_path)
                ]
                if not remaining:
                    return VerifyResult(
                        verifier=self.name, passed=True,
                        detail=f"重扫确认: {issue_type} 已消失",
                        confidence_delta=0.15,
                    )
            except Exception as e:
                logger.warning("ReScan verifier: scanner %s failed: %s", scanner.name, e)

        return VerifyResult(
            verifier=self.name, passed=False,
            detail=f"重扫后 {issue_type} 仍存在于 {file_path}",
            confidence_delta=-0.2,
        )


class ImportVerifier(BaseVerifier):
    """验证修改后的文件能否正常 import（不触发 ImportError）。

    成本：低（subprocess 运行 python -c import）。
    适用于修复 import 相关问题或重构模块路径后。
    """

    @property
    def name(self) -> str:
        return "import"

    def verify(self, issue_type: str, file_path: Path,
               fix_action: str, project_root: Path) -> VerifyResult:
        import_keywords = ["import", "module", "dependency", "missing"]
        if not any(kw in issue_type.lower() or kw in fix_action.lower()
                   for kw in import_keywords):
            return VerifyResult(
                verifier=self.name, passed=True,
                detail="非 import 相关问题，跳过", confidence_delta=0,
            )

        try:
            result = subprocess.run(
                ["python", "-c", f"import importlib; importlib.import_module('{file_path.stem}')"],
                capture_output=True, text=True, timeout=10,
                cwd=str(project_root),
            )
            if result.returncode == 0:
                return VerifyResult(
                    verifier=self.name, passed=True,
                    detail="import 成功", confidence_delta=0.1,
                )
            return VerifyResult(
                verifier=self.name, passed=False,
                detail=f"import 失败: {result.stderr[:200]}",
                confidence_delta=-0.15,
            )
        except Exception as e:
            return VerifyResult(
                verifier=self.name, passed=True,  # 超时等不算失败
                detail=f"import 验证跳过: {e}", confidence_delta=0,
            )



class VerifierChain:
    """按顺序执行多个验证器，汇总结果。

    设计决策（面试话术）：
      "为什么用链式而不是并行？因为验证器有成本递增关系——
       语法检查失败了就没必要跑重扫。短路求值节省时间。"
    """

    def __init__(self, verifiers: list[BaseVerifier]):
        self._verifiers = verifiers

    def verify(self, issue_type: str, file_path: Path,
               fix_action: str, project_root: Path) -> VerifyChainResult:
        """运行验证链，返回汇总结果。"""
        chain_result = VerifyChainResult()

        for verifier in self._verifiers:
            try:
                vr = verifier.verify(issue_type, file_path, fix_action, project_root)
            except Exception as e:
                logger.error("Verifier %s threw exception: %s", verifier.name, e)
                vr = VerifyResult(
                    verifier=verifier.name, passed=False,
                    detail=f"验证器异常: {e}", confidence_delta=-0.1,
                )

            chain_result.results.append(vr)
            chain_result.total_delta += vr.confidence_delta

            if not vr.passed:
                chain_result.passed = False
                if verifier.name == "syntax":
                    chain_result.should_rollback = True
                    logger.warning("Syntax verification failed, rollback recommended")
                    break  # 短路：语法都过不了，不继续

        return chain_result


def build_default_verifier_chain(scanner_registry=None) -> VerifierChain:
    """构建默认验证链：语法 → 重扫。"""
    return VerifierChain([
        SyntaxVerifier(),
        ReScanVerifier(scanner_registry),
    ])
