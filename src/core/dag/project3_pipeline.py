"""项目三 — 默认 DAG 管道定义"""

from src.core.dag.engine import DAGNode, NodeType, DAGPipeline


def build_default_pipeline() -> DAGPipeline:
    """构建项目三默认的扫描→修复 DAG
    
    ┌── arch_scan ──┐
    ├── async_scan ─┤
    ├── config_scan ┼──▶ merge ──▶ analyze ──▶ fix ──▶ verify ──▶ reflect
    ├── perf_scan ──┤
    ├── sec_scan ───┤
    └── quality_scan┘
                (并行扫描)    (汇总)   (分析)   (修复)   (验证)   (反思)
    """
    pipe = DAGPipeline()
    
    pipe.add_node(DAGNode("start", NodeType.TASK))
    
    # 并行扫描节点
    scanners = ["arch_scan", "async_scan", "config_scan", 
                "perf_scan", "sec_scan", "quality_scan", "deadcode_scan",
                "doc_scan", "test_scan"]
    
    for s in scanners:
        pipe.add_node(DAGNode(s, NodeType.TASK, depends_on=["start"]))
    
    # 汇总节点
    pipe.add_node(DAGNode("merge", NodeType.MERGE, depends_on=scanners))
    
    # 条件分支：高危直接走紧急通道
    pipe.add_node(DAGNode("critical_check", NodeType.CONDITION, depends_on=["merge"]))
    
    # 正常通道
    pipe.add_node(DAGNode("analyze", NodeType.TASK, depends_on=["critical_check"]))
    pipe.add_node(DAGNode("fix", NodeType.TASK, depends_on=["analyze"]))
    pipe.add_node(DAGNode("verify", NodeType.TASK, depends_on=["fix"]))
    pipe.add_node(DAGNode("reflect", NodeType.TASK, depends_on=["verify"]))
    
    # 紧急通道（并行）
    pipe.add_node(DAGNode("emergency_fix", NodeType.TASK, depends_on=["critical_check"]))
    pipe.add_node(DAGNode("emergency_verify", NodeType.TASK, depends_on=["emergency_fix"]))
    
    return pipe
