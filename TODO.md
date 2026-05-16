# TODO — 初始种子任务

## Priority: HIGH
- [x] 实现 `swarm_utils.py` 基础工具函数集（包含文件读写、日志辅助函数）
- [x] 更新 README.md 补充架构说明

## Priority: MEDIUM
- [ ] 实现 `swarm_logger.py` 日志记录工具
- [x] 创建一个 SKILL 描述如何安全地提交 Git

## Priority: LOW
- [x] 设计 Agent 互相学习的交叉 Skill 导入机制
- [x] 设计心跳检测机制，确认每个 Agent 健康运行

## 新增任务（Round 11 自动生成）
- [ ] 实现 `swarm_logger.py` 日志记录工具（继承自 MEDIUM 待办）
- [ ] 创建 SKILL 注册表更新流程脚本（`update_registry.py`）
- [ ] 实现 Agent 0 自动化线程安全的 REGISTRY.json 更新
- [ ] 实现 `swarm_health.py` 与 cron 循环的集成（自动心跳 + 错误自动重启）
- [ ] 为 swarm_health.py 补充原子写入和路径白名单加固
- [ ] 更新 cross-skill-learning SKILL 的 REGISTRY.json 示例与实际实现一致
- [ ] 为 git-safe-commit SKILL 补充完整 frontmatter（version/author/license）
