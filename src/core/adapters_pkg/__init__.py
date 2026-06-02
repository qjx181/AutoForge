"""适配器模块"""
from src.core.adapters_pkg.severity import Severity
from src.core.adapters_pkg.issue import Issue
from src.core.adapters_pkg.fix_result import FixResult
from src.core.adapters_pkg.scanner_adapter import ScannerAdapter
from src.core.adapters_pkg.fixer_adapter import FixerAdapter
from src.core.adapters_pkg.scanner_registry import ScannerRegistry
from src.core.adapters_pkg.fixer_registry import FixerRegistry
from src.core.adapters_pkg.legacy_wrapper import LegacyScannerWrapper, IssueIR, IssueProcessor
from src.core.adapters_pkg.legacy_wrapper import build_default_scanner_registry, build_default_fixer_registry
