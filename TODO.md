# TODO — 待办任务

## Priority: HIGH
- [ ] **集成监控仪表盘** — 利用 `swarm_metrics.py` 的数据生成可读的 HTML 监控面板，展示轮次耗时、任务完成率、问题分布等关键指标
- [x] **swarm_config.yaml 示例文件** — 创建标准化的 `config.yaml` 示例配置文件，集成 `swarm_config.py` + `swarm_logger.py` 的配置

## Priority: MEDIUM
- [ ] 为所有现有模块补全类型注解（PEP 484）— `swarm_metrics.py` + `swarm_logger.py` + `swarm_health.py`
- [ ] 实现 `swarm_notifier.py` 通知模块 — 轮次完成/失败时的通知回调机制（支持 stdout/文件/日志）

## Priority: LOW
- [ ] 编写集成测试，模拟完整 A→B→Git 闭环流程
- [x] 清理临时测试脚手架文件（`_verify_swarm_metrics.py` 等）

---

## Round 15+ 进入第三阶段：可观测性与基础设施深化

已完成第二阶段核心模块：
1. ✅ **配置管理** — `swarm_config.py` 配置管理系统（YAML+环境变量+校验）
2. ✅ **可观测性** — `swarm_metrics.py` 指标收集模块（轮次计时/任务追踪/问题统计/持久化/报告生成）

第三阶段目标：
1. **监控可视化** — 将指标数据转化为可读工作面板
2. **配置集成** — 统一所有模块的配置入口
3. **通知机制** — 轮次完成/失败的通知回调
4. **持续质量** — 类型注解、集成测试、脚手架清理
