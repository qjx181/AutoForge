# SWARM 运行规则（2026-05-18 更新）

## 核心变更

**项目三的角色已从「自我进化」转为「护航项目一」**。

即：swarm 不再优化自身代码，而是用自进化循环持续改进项目一。

## 项目一目标目录

- Windows: `C:\Users\qjx\Desktop\agent-自进化版\项目一cursor版本\在线部分\`
- WSL: `/mnt/c/Users/qjx/Desktop/agent-自进化版/项目一cursor版本/在线部分/`

## 当前阶段

Phase 1（高并发优化）和 Phase 2（多路召回+RRF融合）已完成。

当前重点工作：**Phase 3（RAGAS 测试体系）** 和代码质量提升。

## 每轮工作流程

1. 读取 TODO.md 确认待办项
2. A队（dev-cell）执行开发任务
3. B队（qa-cell）审查代码质量
4. 协调者决策：合并/回退/调整
5. Git 提交（本地 commit）
6. 更新 CHANGELOG.md

## 注意事项

- push 到 GitHub 时使用 Windows Git：`F:\人工智能专业\Git\bin\git.exe`
- 所有操作在 agent-自进化版 目录下执行
- 如果国内网络导致 push 失败，仅做本地 commit 即可
