"""DAG 引擎 — 有向无环图执行器"""

from enum import Enum
from typing import Any, Callable, Optional
import asyncio
import logging

logger = logging.getLogger(__name__)


class NodeStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class NodeType(Enum):
    TASK = "task"           # 执行任务
    CONDITION = "condition" # 条件分支
    MERGE = "merge"         # 汇总合并
    APPROVAL = "approval"   # 人工审批


class DAGNode:
    """DAG 节点"""
    
    def __init__(self, name: str, node_type: NodeType = NodeType.TASK,
                 handler: Optional[Callable] = None, depends_on: Optional[list] = None,
                 condition: Optional[Callable] = None):
        self.name = name
        self.type = node_type
        self.handler = handler
        self.depends_on = depends_on or []
        self.condition = condition
        self.status = NodeStatus.PENDING
        self.result: Any = None
        self.error: Optional[str] = None
    
    def ready(self, node_statuses: dict[str, NodeStatus]) -> bool:
        """检查所有依赖是否已完成"""
        return all(node_statuses.get(dep) == NodeStatus.SUCCESS 
                   for dep in self.depends_on)


class DAGPipeline:
    """DAG 管道执行器"""
    
    def __init__(self):
        self.nodes: dict[str, DAGNode] = {}
        self._results: dict[str, Any] = {}
    
    def add_node(self, node: DAGNode) -> Any:
        self.nodes[node.name] = node
        return self
    
    def add_edge(self, fr: str, to: str) -> Any:
        """添加依赖边"""
        if to in self.nodes:
            self.nodes[to].depends_on.append(fr)
        return self
    
    def get_execution_order(self) -> list[str]:
        """拓扑排序 — 确定执行顺序"""
        visited = set()
        order = []
        
        def dfs(node_name: str, path: set) -> Any:
            if node_name in path:
                raise ValueError(f"检测到环路: {node_name}")
            if node_name in visited:
                return
            path.add(node_name)
            node = self.nodes[node_name]
            for dep in node.depends_on:
                if dep in self.nodes:
                    dfs(dep, path)
            path.remove(node_name)
            visited.add(node_name)
            order.append(node_name)
        
        for name in self.nodes:
            if name not in visited:
                dfs(name, set())
        
        return order
    
    async def run(self, context: dict = None) -> dict:
        """执行 DAG"""
        context = context or {}
        order = self.get_execution_order()
        logger.info(f"DAG 执行顺序: {' → '.join(order)}")
        
        # 按轮次执行（每轮执行所有就绪节点）
        completed = set()
        
        while len(completed) < len(self.nodes):
            ready = []
            for name in order:
                if name in completed:
                    continue
                node = self.nodes[name]
                statuses = {n: self.nodes[n].status for n in self.nodes}
                if node.ready(statuses):
                    ready.append(name)
            
            if not ready and len(completed) < len(self.nodes):
                # 检测死锁
                blocked = [n for n in self.nodes if n not in completed]
                for n in blocked:
                    self.nodes[n].status = NodeStatus.BLOCKED
                    self.nodes[n].error = "依赖无法满足"
                break
            
            # 并行执行就绪节点
            tasks = []
            for name in ready:
                node = self.nodes[name]
                node.status = NodeStatus.RUNNING
                
                if node.type == NodeType.CONDITION:
                    # 条件节点：判断后决定走哪条路
                    tasks.append(self._run_condition(node, context))
                elif node.type == NodeType.MERGE:
                    tasks.append(self._run_merge(node, context))
                else:
                    tasks.append(self._run_task(node, context))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(ready, results):
                node = self.nodes[name]
                if isinstance(result, Exception):
                    node.status = NodeStatus.FAILED
                    node.error = str(result)
                    logger.error(f"[DAG] {name} 失败: {result}")
                else:
                    node.status = NodeStatus.SUCCESS
                    node.result = result
                    self._results[name] = result
                completed.add(name)
        
        return self._results
    
    async def _run_task(self, node: DAGNode, context: dict):
        if node.handler:
            if asyncio.iscoroutinefunction(node.handler):
                return await node.handler(context, self._results)
            return node.handler(context, self._results)
        return None
    
    async def _run_condition(self, node: DAGNode, context: dict):
        if node.condition:
            return node.condition(context, self._results)
        return None
    
    async def _run_merge(self, node: DAGNode, context: dict):
        """合并多个上游节点的结果"""
        merged = {}
        for dep in node.depends_on:
            if dep in self._results:
                merged[dep] = self._results[dep]
        return merged
    
    def get_status(self) -> dict:
        return {n: self.nodes[n].status.value for n in self.nodes}
    
    def to_text(self) -> str:
        """文本可视化"""
        lines = ["DAG Pipeline:"]
        order = self.get_execution_order()
        for name in order:
            node = self.nodes[name]
            deps = f" ← {','.join(node.depends_on)}" if node.depends_on else ""
            status = f" [{node.status.value}]"
            lines.append(f"  {name}{deps}{status}")
        return "\n".join(lines)
