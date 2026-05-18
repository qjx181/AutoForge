     1|# CHANGELOG
     2|
     3|## Round 0 — 系统初始化 (D1)
     4|- 创建项目目录结构（F:\项目三：多Agent\）
     5|- 写入 SWARM_RULES.md（完整运行规则）
     6|- 写入 TODO.md（初始种子任务）
     7|- 创建 3 个核心 SKILL（orchestrate-swarm / dev-cell / qa-cell）
     8|- 写入 README.md 和 CHANGELOG.md
     9|- 写入 self_evolve_round.py 协调者脚本
    10|- 初始化 Git 仓库
    11|
    12|## Round 1 — 20260515_153407
    13|- 完成: 执行 Round 1 状态审计
    14|- 摘要: 协调者状态审计，检测到 7 个待办任务
    15|
    16|## Round 2~10 — 20260515_210001 ~ 20260516_110001（空转期，已合并）
    17|- 状态: Hermes cronjob 空转 9 轮，仅执行状态审计未执行实质开发
    18|- 修复: Round 12 重建 cronjob 后恢复正常
    19|
    20|## Round 11 — 20260516_111306 (Swarm 进化首轮)
    21|- 完成: 更新 README.md 补充架构说明, 创建 git-safe-commit SKILL, 创建 cross-skill-learning SKILL, 实现 swarm_health.py 心跳检测
    22|- 新增: README.md (28→404行, 含架构图/角色表/演进路线图), git-safe-commit SKILL (devops), cross-skill-learning SKILL (software-development), swarm_health.py (心跳检测/健康监控)
    23|- 审查: B队4 Agent 审查通过所有 A 队产出 (Agent 5-8)
    24|- 决策: 全部批准合并，修复 README 轮次编号 + .gitignore 心跳目录
    25|- 摘要: A 队 4 Agent 首次并行开发 —— 更新 README、创建 2 个新 SKILL（Git 安全提交 / 跨技能学习）、实现心跳健康检测模块。B 队审查发现 3 个 revision_needed（README 编号同步、SKILL 元数据补全、安全加固）、1 个 approve（swarm_health.py 9/10）。协调者修复关键问题后合并。push 失败（国内网络），本地 commit 已完成。
    26|
    27|## Round 12 — 20260516_111111 (Cronjob 修复 + 首轮 A→B→Git 闭环验证)
    28|- 完成: 重建 Hermes cronjob (swarm-evolve-round) 带 skills 加载, 手动触发 A队→B队→Git 闭环测试
    29|- 修复: self_evolve_round.py 从空转审计改为状态报告脚本, 删除已停用的系统 cron
    30|- 更新: README.md 再由 A队补充架构说明 (404→636行), B队审查评分 8.5/10, PASS
    31|- 新增: tmux daemon (hermes-swarm) 已启动常驻
    32|- 同步: TODO.md 更新反映真实进度（git-safe-commit/cross-skill-learning/swarm_health 标记为完成）
    33|- 摘要: 修复项目三核心问题 —— Hermes cronjob 从 5月15日22:02 后停止工作, Round 6-10 空转。删除旧 cronjob 重建为带 skills (orchestrate-swarm/dev-cell/qa-cell) 和完整 prompt 的版本。手动验证一轮 A→B→Git 闭环通过。tmux daemon 已启动让 cronjob 可以自动触发。
    34|
    35|## Round 13 — 20260517_194100
    36|- 完成: 实现 swarm_logger.py 结构化日志记录工具
    37|- 新增: swarm_logger.py (436行) — SwarmLogger 类, 5级日志, TEXT/JSON 双格式, RotatingFileHandler 文件轮转, **extra 结构化字段, CLI 入口
    38|- 审查: B队 Agent 5 审查评分 8.5/10, 发现 1 个高危(JSON序列化容错)+3 个中危(线程安全/异常保护), 协调者修复后合并
    39|- 修复: JsonFormatter json.dumps 加 default=str + try/except; log() 加 try/except 保护; handlers 遍历用 list() 快照防并发; 删除死代码 _extra_local; .gitignore 排除 logs/ 目录
    40|- 摘要: A 队 Agent 1 实现 swarm_logger.py 日志记录工具 —— 支持 DEBUG~CRITICAL 5 级别、TEXT/JSON 双输出格式、按文件大小自动轮转、可配置路径和级别、结构化 extra 字段。B 队审查发现 7 个问题(1 高危、3 中危、3 低危)，协调者修复关键问题后合并。同步完善 .gitignore 排除 logs/ 目录并移除已跟踪的日志文件。swarm_logger 现可被其他模块直接 import 使用。push 失败（国内网络），本地 commit 已完成。
    41|
    42|## Round 14 — 20260517_202000
    43|- 完成: 为全部 3 个核心模块（swarm_utils / swarm_logger / swarm_health）编写完整 pytest 单元测试
    44|- 新增: test_swarm_utils.py（16测试）、test_swarm_logger.py（35测试）、test_swarm_health.py（41测试）—— 共 92 个测试全部通过
    45|- 审查: B队 Agent 5 审查评分 9.3/10, 裁决 PASS（仅 2 个 cosmetic/minor 问题），无需修改直接合并
    46|- 清理: 合并 Round 2~10 空转条目为单条记录
    47|- 里程碑: 所有初始 TODO 任务全部完成
    48|- 摘要: Round 14 完成单元测试体系建设——使用 pytest + tmp_path fixture 为 swarm_utils.py（文件读写工具）、swarm_logger.py（结构化日志）、swarm_health.py（心跳检测）三个模块编写了 92 个单元测试，覆盖正常路径、边界条件、异常情况和 CLI 入口。测试使用临时目录避免污染项目文件系统。B 队审查高度评价（9.3/10），无阻塞问题直接合并。至此所有初始 TODO 任务全部标记为完成。push 失败（国内网络），本地 commit 已完成。
    49|
    50|## Round 15 — 20260517_213400
    51|- 完成: 实现 swarm_metrics.py 指标收集模块（1073行），包含 RoundTimer/TaskTracker/IssueTracker/MetricsStore/MetricsReporter 五个核心组件 + SwarmMetrics 聚合类
    52|- 修复: 回溯标记 TODO.md 中实际已完成的 swarm_config.py 为 [x]
    53|- 审查: B队 Agent 5 审查评分 7.5/10, 裁定 NEEDS_FIXES（2 ERROR + 4 WARNING + 4 INFO）
    54|- 修复: 协调者修复 2 个 ERROR（import sys 作用域错误 + duration_sec None 值类型错误）后合并
    55|- 新增: swarm_metrics.py（完整指标收集模块）, swarm_config.py 首次被 git 跟踪（此前未被提交过）
    56|- 更新: TODO.md 进入第三阶段——可观测性与基础设施深化（监控仪表盘/配置集成/通知模块/类型注解）
    57|- 摘要: Round 15 实现指标收集模块——A 队 Agent 1 使用 DeepSeek 实现覆盖 5 个组件类的完整 API（start_round/end_round/record_task/record_issue/save/load/generate_report），B 队审查发现 2 个运行时崩溃风险（#E001: import sys 在 `__main__` 内导致 NameError；#E002: dict.get() None 值引发 TypeError）和 4 个设计问题。协调者修复关键问题后合并。swarm_config.py（785 行）也首次被纳入版本控制——此前已完成但未提交过。TODO 进入第三阶段，新增 4 个新任务。push 失败（国内网络），本地 commit 已完成。
    58|
    59|## Round 16 — 20260517_221500
    60|- 完成: 创建 config.yaml 标准化示例配置文件（208行），集成 swarm_config.py + swarm_logger.py + swarm_metrics.py 的配置
    61|- 新增: config.yaml（208行）— 包含 swarm/agents/logger/metrics/git 5个配置模块，所有字段均有详细英文注释和类型说明
    62|- 审查: B队 Agent 5 审查评分 100/100，PASS，无任何问题
    63|- 决策: 直接合并
    64|- 清理: 排除 A 队遗留的 check_yaml.py 临时验证文件，仅保留 config.yaml 到 Git
    65|- 摘要: Round 16 创建标准化 YAML 示例配置文件——A 队 Agent 1 实现覆盖 swarm/agents/logger/metrics/git 5 个模块、20 个字段的完整 YAML 配置示例，每个字段附带类型/默认值/用途注释。B 队审查满分通过（100/100），无阻塞问题直接合并。push 失败（credential issue），本地 commit 已完成。
    66|
    67|## Round 17 — 20260518_102000（项目一开发工单执行）
    68|- 完成: 执行项目一（多角色RAG聊天系统）开发工单的阶段一（高并发优化）和阶段二（多路召回+混合检索）
    69|- 阶段一（4个子任务）:
    70|  - ✅ **LLM异步化** — llm_client.py 改用 httpx.AsyncClient 异步客户端 + asyncio.sleep 替代 time.sleep，修复 sync generator 被 async for 调用的运行时错误
    71|  - ✅ **并发控制** — middleware/rate_limit.py: TokenBucket 令牌桶限流（20qps/桶容量40）+ asyncio.Semaphore 并发控制（最大8并发）+ 429/503友好JSON响应
    72|  - ✅ **Redis连接池优化** — memory.py: 显式 ConnectionPool（max_connections=20, socket_keepalive, retry_on_timeout, health_check_interval=30）
    73|  - ✅ **Milvus连接池** — milvus_pool.py: 统一连接管理（pool_size=10, retry=3），main.py lifespan 启动时初始化，retrieval.py/knowledge_store.py 改用连接池
    74|- 阶段二（3个子任务）:
    75|  - ✅ **RRF融合** — retrieval.py: rrf_fusion() 实现倒数排名融合（k=60），替代原来的简单扩展+排序，每个来源的排名被正确加权
    76|  - ✅ **超时控制与降级** — 每路检索来源独立超时（semantic 15s/vector 20s/web 30s/chat_kb 10s），超时不阻塞整体检索
    77|  - ✅ **Redis缓存层** — services/retrieval_cache.py: 独立 DB1 存储检索结果（TTL 5min），仅缓存有结果的数据
    78|- 变更总量: 12 个文件，+779/-326 行，2 次 Git 提交
    79|- 待办: Phase 3（RAGAS测试体系）尚未开始
    80||- 经验: qwen2.5:7b 作为子 Agent 在保持函数签名一致性上表现不佳（llm_client.py 改错），协调者直接 write_file 修复。对于需要精确接口兼容的任务，协调者应直接操作而非委托。`rm -rf` 被安全策略拦截，子 Agent 也无法绕过。|
    81|
    82|## Round 19 — 20260518_151500（项目一阶段三：RAGAS评估框架+单元测试）
    83|- 完成: 3个TODO任务实现并提交到外部项目一
    84|- 子任务1 ✅ **RAGAS评估框架** — evaluation/ragas_evaluator.py（280行），4项核心指标（faithfulness/answer_relevancy/context_precision/context_recall），ragas库不可用时自动降级统计指标，JSON+TXT双格式报告，超时控制
    85|- 子任务2 ✅ **令牌桶+信号量测试** — tests/test_rate_limit.py，TokenBucket 9项测试（构造/消耗/恢复/突发/零速率），Semaphore基础验证
    86|- 子任务3 ✅ **Milvus连接池测试** — tests/test_milvus_pool.py，8项测试覆盖初始化/连接/缓存/存在性检查
    87|- 变更: 项目一 +4文件 +445行，Git commit 7beef1a
    88|- 经验: qwen2.5:7b作为子Agent产出代码质量差——ragas_evaluator.py为空文件，两个测试文件均有语法和逻辑错误。协调者直接write_file重写3个文件后17/17测试通过。对于测试类和框架类任务，协调者直接编写比委托效率更高。B队子Agent因中文路径无法完成审查。
    89|
    90|
    91|

## Round N — 20260518_194000 — 虚假提交根因修复(方案A+B+C组合)
- 根因诊断：cronjob prompt 5步流程跳过 diff_content_check，协调者用 qwen2.5:7b 无法正确调用工具
- 方案A(验证增强)：orchestrate-swarm SKILL.md Step 3 强化 diff_content_check 指引，优先级高于其他验证
- 方案A(验证增强)：dev-cell SKILL.md report.json 新增 function_signatures 字段，让协调者能验证接口签名
- 方案A(验证增强)：cronjob prompt 新增 Step 3（产出校验），Step 2→Step 3 之间插入 diff 内容验证
- 方案C(模型升级)：协调者模型从 qwen2.5:7b(本地) 切换为 deepseek-v4-flash(云端)
- 方案B(直接写入示范)：协调者 write_file 直接修复 services/retrieval.py 同步→异步转换
  - asyncio.to_thread 替代 ThreadPoolExecutor，asyncio.wait_for 替代 fut.result(timeout)
  - 保留 sync 包装器 build_context_sync 供迁移过渡
  - 更新 4 个调用方文件（chat_service/chat_pipeline/debug/test）导入 build_context_sync
  - 项目一 commit: 58f8242，5 files changed, +404/-306