# AutoForge — Hermes Agent 配置

自进化多Agent代码质量引擎。可持续运行，以后工作也能用。

## 能力总览

```
┌──────────────────────────────────────────────┐
│          项目三：多Agent 自进化引擎           │
├──────────────────────────────────────────────┤
│  🔍 深度扫描   → 发现代码问题（9个维度）     │
│  🔧 自动修复   → 修复6类常见代码质量问题      │
│  📊 成本控制   → $5/天预算，三级熔断           │
│  🎯 目标灵活   → 指向任意项目                 │
│  📈 渐进优化   → 每轮扫描→修复→反思→更好      │
│  🎭 多角色     → 169个 agency-agents 技能可用  │
└──────────────────────────────────────────────┘
```

## 可用 Agent 角色

| 角色 | 用于 | 加载命令 |
|------|------|---------|
| **Code Reviewer** | 代码审查、质量门禁 | `skill_view("agency-code-reviewer")` |
| **AI Engineer** | AI/ML相关代码分析 | `skill_view("agency-ai-engineer")` |
| **Backend Architect** | 架构设计审查 | `skill_view("agency-backend-architect")` |
| **Evidence Collector** | QA测试、问题复现 | `skill_view("agency-evidence-collector")` |
| **Reality Checker** | 可行性判断、防过度优化 | `skill_view("agency-reality-checker")` |
| **Self-Optimization** | 系统自身的持续改进 | `skill_view("self-optimization-agent")` |
| **DevOps Automator** | CI/CD、部署 | `skill_view("agency-devops-automator")` |
| **Security Engineer** | 安全审计 | `skill_view("agency-security-engineer")` |

## CLI 操作

```
p3 status      查看系统状态
p3 cost        查看成本报告
p3 scan <目录> 手动扫描
p3 setup <目录> 注册优化目标
p3 cron on|off 控制自动循环
```

## 自动化任务

| 任务 | 频率 | 说明 |
|------|------|------|
| 自进化轮次 | 每2小时 | 后勤检查 + 目标同步 + 成本熔断 |
| 全项目质量扫描 | 周一/三/五 | 快速质量扫描 |
| 项目代码审查 | 每周一 | 深度代码审查（当前目标项目） |
| 技能自增强 | 每周日 | 审计增强所有 agency-* 技能 |
| 技能发现 | 每月1号 | 扫描GitHub新技能源 |
| 技能清理 | 每月1号/15号 | 删除低质/未使用技能 |
