"""Agent 分层角色定义模块。

定义 3 个专业化 Agent 角色 + 5 个预留槽位的约束和行为规范。
每次委托时，协调者应根据 task_type 选择对应角色，并注入角色模板。
"""

from dataclasses import dataclass, field
from typing import Optional

# ============ 角色定义 ============

@dataclass
class AgentRole:
    """单个 Agent 角色的完整定义。"""
    role_id: str
    name: str
    description: str
    allowed_tasks: list[str]
    forbidden_actions: list[str]
    required_inputs: list[str]
    template_file: str


AGENT_CODER = AgentRole(
    role_id="agent-coder",
    name="编码者",
    description="按模板写代码，不做架构决策",
    allowed_tasks=["function_body_replacement", "test_fill_template", "docstring_update"],
    forbidden_actions=[
        "修改函数签名（参数名、参数类型、返回值类型）",
        "删除文件",
        "修改接口定义",
        "自行决定改造方案",
    ],
    required_inputs=["目标文件路径", "改动要求描述", "示例代码片段"],
    template_file="templates/coder_template.md",
)

AGENT_REVIEWER = AgentRole(
    role_id="agent-reviewer",
    name="审查者",
    description="diff 检查，不写代码",
    allowed_tasks=["diff_signature_check", "test_coverage_audit", "dead_code_detection"],
    forbidden_actions=[
        "修改任何文件",
        "生成代码",
    ],
    required_inputs=["改动前文件内容", "改动后文件内容"],
    template_file="templates/reviewer_template.md",
)

AGENT_TESTER = AgentRole(
    role_id="agent-tester",
    name="测试者",
    description="按模板编写测试用例",
    allowed_tasks=["unit_test_creation", "test_case_expansion", "mock_template_fill"],
    forbidden_actions=[
        "修改被测源代码",
        "更改测试框架配置",
    ],
    required_inputs=["被测文件路径", "函数列表", "mock 模板示例"],
    template_file="templates/tester_template.md",
)

# 弹性资源（agent 4~6）：用于突发任务/高负载
ELASTIC_SLOTS = [
    AgentRole(
        role_id="agent-burst-1",
        name="弹性执行者-1",
        description="突发任务执行（未分配固定角色）",
        allowed_tasks=[],
        forbidden_actions=[],
        required_inputs=[],
        template_file="",
    ),
    AgentRole(
        role_id="agent-burst-2",
        name="弹性执行者-2",
        description="突发任务执行（未分配固定角色）",
        allowed_tasks=[],
        forbidden_actions=[],
        required_inputs=[],
        template_file="",
    ),
    AgentRole(
        role_id="agent-burst-3",
        name="弹性执行者-3",
        description="突发任务执行（未分配固定角色）",
        allowed_tasks=[],
        forbidden_actions=[],
        required_inputs=[],
        template_file="",
    ),
]

# 未来扩展槽位（agent 7~8）
FUTURE_SLOTS = [
    AgentRole(role_id="agent-future-1", name="预留-1", description="未来扩展", allowed_tasks=[], forbidden_actions=[], required_inputs=[], template_file=""),
    AgentRole(role_id="agent-future-2", name="预留-2", description="未来扩展", allowed_tasks=[], forbidden_actions=[], required_inputs=[], template_file=""),
]

# ============ 工具函数 ============


def get_role(role_id: str) -> Optional[AgentRole]:
    """根据 role_id 获取角色定义。"""
    for role in ALL_ROLES:
        if role.role_id == role_id:
            return role
    return None


def match_role(task_type: str) -> Optional[AgentRole]:
    """根据任务类型匹配合适的角色。"""
    for role in ALL_ROLES:
        if task_type in role.allowed_tasks:
            return role
    return None


ALL_ROLES = [
    AGENT_CODER,
    AGENT_REVIEWER,
    AGENT_TESTER,
    *ELASTIC_SLOTS,
    *FUTURE_SLOTS,
]
