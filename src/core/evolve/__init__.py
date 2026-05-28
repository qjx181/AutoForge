"""自进化主循环 — 拆分为 evolve/ 包"""
from src.core.evolve.pipeline import run_optimization_pipeline
from src.core.evolve.logging import relog
from src.core.evolve.state import acquire_pid_file
from src.core.evolve.state import release_pid_file
from src.core.evolve.state import load_state
from src.core.evolve.state import save_state
from src.core.evolve.cost import check_disk_space
from src.core.evolve.cost import check_cost_over_budget
from src.core.evolve.git_ops import git_pull_rebase
from src.core.evolve.git_ops import run_git_commit
from src.core.evolve.git_ops import run_git_commit_with_retry
from src.core.evolve.git_ops import check_and_heal_conflicts
from src.core.evolve.git_ops import mark_conflict
from src.core.evolve.delegation import run_delegation_diagnosis
from src.core.evolve.delegation import check_forced_delegation
from src.core.evolve.cli import main
