# TODO — 项目三护航任务：持续改进项目一

> 项目三现在的工作目标：用 swarm 自主驱动对项目一的持续改进和优化。
> 项目一目录：`C:\\Users\\qjx\\Desktop\\agent-自进化版\\项目一cursor版本\\在线部分\\`

---

## ✅ 已完成确认（2026-05-18 代码审计验证）

以下 Phase 1/Phase 2 优化经代码审计确认为已完成，不再重复派发：

**阶段一 — 高并发优化**
- [x] LLM调用 httpx.AsyncClient — llm_client.py 已使用 httpx.AsyncClient + 共享连接池
- [x] Milvus连接池 — milvus_pool.py pool_size=10，get_cached_collection 缓存
- [x] Redis连接池 — memory.py redis.ConnectionPool + max_connections=20
- [x] Semaphore并发控制 — middleware/rate_limit.py asyncio.Semaphore(8)，routes/chat.py 调用
- [x] 令牌桶限流 — middleware/rate_limit.py class TokenBucket + CHAT_RATE_LIMITER
- [x] 503友好提示 — routes/chat.py _503_MESSAGE + JSONResponse(status_code=503)
- [x] 路由层 async def（主路由） — routes/chat.py 已 async

**阶段二 — 多路召回**
- [x] RRF融合策略 — services/retrieval.py rrf_fusion()，_RRF_K=60 标准实现
- [x] 超时控制 — _SOURCE_TIMEOUTS 各来源独立超时 + asyncio.wait_for + asyncio.to_thread
- [x] Redis缓存检索结果 — services/retrieval_cache.py，get/set_cached_result，TTL可配
- [x] 结果截断 — 字符级截断 short_content[:1200]/excerpt[:800]（但按 token 数非字符数，见下方优化项）

**阶段三 — RAGAS**
- [x] RAGAS评估框架 — evaluation/ragas_evaluator.py 270行，4项指标
- [x] add_regression_test_suite — Round 20 完成
- [x] add_rate_limit_tests — 完成
- [x] add_milvus_pool_tests — 完成

**代码质量**
- [x] cleanup_duplicate_chat_code — chat_pipeline.py 已标注 deprecated（受 chat_service.py 完全覆盖）
- [x] auth验证加固 — 空用户名/短密码/重复注册 校验
- [x] 测试覆盖增强 — test_web_fallback(16)+test_session(7)+test_routes_auth(12) = 35个测试

---

## Priority: HIGH

- [x] 任务ID: ragas_install_and_integrate
  描述: 安装 ragas + datasets 库，配置 LLM-as-judge 裁判，将 RagasEvaluator 集成到项目一主流程
  验收标准:
    - 项目一环境已安装 ragas 和 datasets（pip install）
    - 配置 LLM 裁判：让 RAGAS 使用 DeepSeek API 或本地 Ollama 做 LLM-as-judge 打分
    - 在 evaluation/ 下创建 run_ragas_eval.py 单次运行入口
    - 在 services/ 中创建 evaluation_service.py，在每次对话结束时异步触发 RagasEvaluator.evaluate_single()
    - 评估结果写入 logs/ragas/ 目录，按日期分文件
  依赖: build_ragas_evaluator（已存在，基于它做集成）
  预估 token 量: 3500

- [x] 任务ID: add_stress_test_suite
  描述: 编写压力测试套件，验证系统在 50 并发下的 P95 响应时间 < 10s，成功率 > 95%
  验收标准:
    - 使用 httpx.AsyncClient + asyncio.gather 模拟并发
    - 令牌桶耗尽、Semaphore 槽位占满、缓存穿透 3 种场景独立测试
    - ✅ 全部 7 项测试通过（1.2s），P95 < 200ms
  依赖: 无
  预估 token 量: 2000

- [x] 任务ID: asyncify_small_routes
  描述: 将 routes/session.py 和 routes/auth.py 从 sync def 改为 async def
  验收标准:
    - routes/session.py 所有路由改为 async def
    - routes/auth.py 所有路由改为 async def
    - 同步 I/O 调用使用 asyncio.to_thread 或直接 await
    - 通过所有现有测试
  依赖: 无
  预估 token 量: 2500

- [x] 任务ID: aggressive_truncation_by_tokens
  描述: 将结果截断从字符数改为按 token 数截断，使用更激进的策略
  验收标准:
    - 使用近似分词（tiktoken 或自定义 token 估算）替代字符数切片
    - short_content 截断为 ~300 tokens，excerpt ~200 tokens，preview ~100 tokens
  依赖: 无
  预估 token 量: 2000

## Priority: MEDIUM

- [x] 任务ID: introduce_jieba_tokenizer
  描述: 引入 jieba 分词替换 BM25 的 .split() 空格分词，提升中文召回率
  验收标准:
    - data_loader.py 中 tokenize() 使用 jieba.lcut 替代 .split()
    - 保留英文原样按空格分词，仅中文使用 jieba
  依赖: 无
  预估 token 量: 1500

## Priority: LOW

- [x] 任务ID: tune_cache_params
  描述: 调优 lru_cache 模块级缓存大小参数（如 cached_encode maxsize=8192）
  验收标准:
    - 分析各缓存的热点数据量，为每个缓存设置匹配的 maxsize
    - 通过所有现有测试
  依赖: 无
  预估 token 量: 500

---

## 破局任务（Round 29 扫描生成）

### Priority: HIGH

- [ ] 任务ID: metrics_sqlite_storage
  描述: 将指标存储从 JSONLines 扩展为 SQLite 原生存储，增加按时间范围聚合查询
  验收标准:
    - 创建 metrics.db SQLite 数据库（与 cost_tracker.db 分开或合并，协调者自定）
    - swarm_metrics.py 的指标写入逻辑增加 SQLite 适配，兼容原有 JSONLines 写入
    - 支持按时间范围聚合查询（如: 最近24h/7d/30d 的调用量、耗时代码、成功率）
    - 启动时自动建表，异常降级到 JSONLines 文件模式
    - 新增 metrics_query.py 模块对外提供查询接口
  注意: 项目三自身代码（swarm_metrics.py），与 cost_tracker_persistence 有相似性，可考虑统一 SQLite 工具层
  依赖: 与 cost_tracker_persistence 共用 SQLite 基础设施
  预估 token 量: 2500

- [ ] 任务ID: git_autopush_safety
  描述: 启用 auto_push 前增加分支保护检查，避免误操作覆盖远程代码
  验收标准:
    - 在 self_evolve_round.py 的 git push 逻辑中增加分支检查
    - 检测当前分支是否为 main/master/protected 开头的分支
    - protected 分支上禁止 autopush，输出警告日志并跳过
    - 检查远程是否有未拉取的提交（git fetch + git status ahead/behind 检测）
    - 有 ahead/behind 冲突时输出日志并跳过
    - 所有检查通过后才执行 git add / commit / push
  注意: 项目三自身代码。config.yaml 已有 git.auto_push: false 字段
  依赖: git shell 命令可用
  预估 token 量: 1500

- [ ] 任务ID: cost_tracker_persistence
  描述: 将成本跟踪改为 SQLite 持久化存储，支持跨日累计和成本趋势查询
  验收标准:
    - 创建 cost_tracker.db SQLite 数据库，表结构包含 timestamp/date/cost/provider/model/task_id
    - self_evolve_round.py 的 cost_tracker 逻辑改为读写 SQLite 而非仅 state.json
    - 支持按日期查询历史成本（如: 最近7天趋势）
    - 启动时自动建表，异常降级到内存模式
    - 保留 state.json 中的 cost_tracker 字段作为缓存
  注意: 项目三自身代码（self_evolve_round.py），协调者可直接修改
  依赖: 无
  预估 token 量: 2500

- [ ] 任务ID: heartbeat_self_healing
  描述: 心跳超时后自动尝试重启失联 agent（通过 PID 文件）
  验收标准:
    - 读取 config.yaml 的 heartbeat_timeout_seconds 和 heartbeat_dir
    - 检测到某 agent 心跳超时（PID 文件存在但文件未更新 > timeout）
    - 自动 kill 原进程后通过 subprocess 重启
    - 记录重启事件到恢复日志（包含时间、agent 名、PID）
    - 单轮最多重启 3 个 agent 防恶性循环
    - 通过 tests/test_heartbeat.py 全部测试
  注意: 项目三自身代码。README.md 提到了 swarm_health.py 但不存在
  依赖: self_healing 配置参数（config.yaml 已有）
  预估 token 量: 2000

- [x] 任务ID: asyncify_knowledge_store
  描述: 将 services/knowledge_store.py 的 sync I/O 函数改为 async def + asyncio.to_thread
  验收标准:
    - 所有 sync I/O 函数增加 asyncio.to_thread 包装（pymilvus 是同步库）
    - 保持函数签名完全不变
    - 不改变 _row_to_ref 等纯计算函数（保持 sync）
    - 通过 tests/test_knowledge_store.py 全部测试
  依赖: 无
  预估 token 量: 2000

### Priority: MEDIUM

- [x] 任务ID: cleanup_deprecated_chat_pipeline
  描述: 删除已废弃的 services/chat_pipeline.py（被 chat_service.py 完全覆盖），更新所有 import 引用
  验收标准:
    - chat_pipeline.py 文件被删除（git rm）
    - 项目中无任何文件 import chat_pipeline
  依赖: 无
  预估 token 量: 500

|- [x] 任务ID: asyncify_session_service
  描述: 将 services/session_service.py 的 SessionService.create_session 改为 async def
  验收标准:
    - create_session 改为 async def
    - 内部同步 I/O 调用（memory 操作）使用 asyncio.to_thread
    - 通过 tests/test_session.py 全部测试（13 tests ✅）
  依赖: 无
  预估 token 量: 1500

- [x] 任务ID: sandbox_pool_retry_and_alert
  描述: 增强 start_sandbox_pool.sh 容器池稳定性——启动重试(3次指数退避)+失败告警写入日志
  验收标准:
    - start 操作失败时自动重试 3 次，间隔 1s/2s/4s 指数退避
    - 3 次均失败时写入 logs/sandbox_pool_error.log，格式: [时间戳] FAILED: 失败原因
    - status 检查失败时同样记录告警日志
    - 可考虑创建 container_pool.py Python 封装，提供更健壮的重试/日志/健康检查
  注意: container_pool.py 不存在，项目只有 start_sandbox_pool.sh。协调者自行决定是加固 shell 还是创建 Python 封装。
  依赖: 无
  预估 token 量: 1000

- [ ] 任务ID: json_logs_startup_flag
  描述: 增加 --json-logs 启动参数，生产环境可快速开启 JSON 日志格式
  验收标准:
    - self_evolve_round.py 增加 argparse 参数 --json-logs（默认 False）
    - --json-logs 开启时，logger 的 handler 格式改为 JSON 序列化（json.dumps）
    - 保持默认日志格式不变（纯文本），只在显式传参时切换
    - 不影响现有日志文件和日志级别
  注意: 项目三自身代码。config.yaml 已有 json_mode: false
  依赖: 无
  预估 token 量: 800

---

## 项目三自身优化（Round 30 起 — 自主驱动自身迭代）

> 说明：以下任务目标为优化项目三自身代码，使其持续自主进化。
> 项目三根目录：`F:\\项目三：多Agent\\`

### Priority: HIGHEST

- [x] 任务ID: define_agent_roles
  描述: 定义 Agent 分层角色——将 8 个 Agent 重新定义为 3 个专业化角色，不再模糊指派
  验收标准:
    - ✅ agent_roles.py 已创建：定义 3 个角色（agent-coder / agent-reviewer / agent-tester）+ 5 预留槽位
    - 各角色含 allowed_tasks/forbidden_actions/required_inputs/template_file
    - 提供 get_role() 和 match_role() 工具函数
  依赖: 无（已完成 ✅）
  预估 token 量: 3000

- [ ] 任务ID: define_layered_delegation
  描述: 新增分层委托流程——协调者只负责 Layer 1，将 Layer 2 委托给 Agent
  验收标准:
    - 在 self_evolve_round.py 或 delegate_optimizer.py 中实现三层决策：
      Layer 1（协调者必须自己干）：
        · 扫描代码库，决定"改什么"
        · 设计接口，定义"怎么改"
        · 编写代码模板和约束条件
        · 验收 Agent 产出（diff_content_check + pytest）
      Layer 2（委托 Agent 干）：
        · 按模板替换指定的函数体（接口不变）
        · 补充测试用例（按模板格式）
        · 整理文档（按格式填空）
        · 运行指定命令验证
      Layer 3（协调者验收）：
        · diff_content_check：确认改动符合预期
        · pytest 验证通过
        · lint 检查通过
        · 不通过则打回 Layer 2 重做
    - 派发 prompt 中强制含 5 条约束：
      1. 【禁止】修改任何函数签名
      2. 【禁止】删除任何文件，只能修改内容
      3. 【必须】在改动前先 read_file 读取完整文件
      4. 【必须】用 StrReplace 工具精确替换，不许用 Write 覆盖整文件
      5. 【必须】改动完成后运行 python -m py_compile <文件路径>
    - 违反任一条→直接拒绝产出
  依赖: define_agent_roles（先有角色定义才有分层流程）
  预估 token 量: 3500

- [ ] 任务ID: diagnose_subagent_failure
  描述: 诊断子 Agent 为什么不干活——分析 self_evolve_log.json 中所有 delegate 条目，统计成功率/失败模式，写入 diagnosis 字段
  验收标准:
    - 扫描 self_evolve_log.json 的 "rounds" 数组中所有含 delegate/subagent 的条目
    - 统计：总委托次数、成功次数、失败次数、成功率
    - 归因分析：失败原因是模型太弱？指令不够清晰？任务拆分不合理？协调者过度自信？
    - 产出诊断报告，写入 self_evolve_log.json 的 diagnosis 字段
    - 为协调者行为偏差写反思记录（是否所有任务都自己干，不信任子 Agent）
  预估 token 量: 1500

- [ ] 任务ID: create_delegate_optimizer
  描述: 创建 delegate_optimizer.py 委托策略优化器——让 swarm 有能力评估"该不该委托"
  验收标准:
    - 新增文件 delegate_optimizer.py，实现决策逻辑：
      if task_complexity < COMPLEXITY_THRESHOLD and agent_success_rate > 0.6:
          delegate_to_subagent()
      else:
          coordinator_handles_directly()
    - 跟踪三个关键指标：agent_success_rate（近10轮）、task_complexity（按token分级：<1000简单、1000-3000中等、>3000复杂）、round_pressure（预算剩余比例）
    - 预算充足时多委托试错，预算紧张时保守
    - 集成到 self_evolve_round.py 的主循环中
  预估 token 量: 3000

- [ ] 任务ID: create_parallel_dispatcher
  描述: 创建 parallel_dispatcher.py 并行任务分发器——打破串行模式，同时跑3个Agent
  验收标准:
    - 新增文件 parallel_dispatcher.py
    - 分配策略：Agent-1 拿 HIGH 优先级任务，Agent-2 拿 MEDIUM，Agent-3 拿 LOW（探索性）
    - 协调者拿"需要精确接口保持"的任务（sync→async 改造、测试文件）
    - 并发上限 3 个 Agent，超时控制 600s
    - 集成到 self_evolve_round.py 的主循环中
    - 效果验证：从串行1个任务变成同时跑3个Agent，吞吐量×3
  依赖: diagnose_subagent_failure（先诊断再改造）, create_delegate_optimizer
  预估 token 量: 3500

- [ ] 任务ID: create_agent_capability_map
  描述: 创建 agent_capability_map.json 能力画像文件，每次委托后记录结果，建立各 Agent 的能力画像
  验收标准:
    - 新增文件 agent_capability_map.json，结构按 task_type 索引：{agent_id, task_type, success_rate, avg_tokens, failure_pattern}
    - 每次 delegate 完成后更新画像（成功/失败、消耗 token、失败原因）
    - 协调者动态选择 Agent 逻辑：
      success_rate > 0.5 → 优先派任务
      success_rate < 0.3 → 仅派简单任务
      rate=0（新 Agent）→ 派探索性/低风险任务
    - 集成到 delegate_optimizer.py 的选择逻辑中
  依赖: create_delegate_optimizer
  预估 token 量: 2500

- [ ] 任务ID: forced_delegation_rule
  描述: 在 self_evolve_round.py 增加强制委托规则——每轮至少委托 1 个任务给子 Agent
  验收标准:
    - 每轮主循环结束时检查本轮 delegate 次数
    - 如果 delegate_count == 0，从剩余任务中强制选 1 个低风险任务委托
    - 即使协调者觉得"自己干更快"也要执行——多 Agent 系统的价值在探索多样性
    - 记录强制委托事件到 self_evolve_log.json
  依赖: create_delegate_optimizer, create_agent_capability_map
  预估 token 量: 1500

- [ ] 任务ID: delegation_validation_loop
  描述: 每轮自动验证委托指标闭环——子 Agent 委托率≥30%、成功率≥50%、并行数≥2、协调者直接操作率≤70%
  验收标准:
    - 每轮结束后自动检查 4 项指标并写入 state.json 的 metrics_validation 字段
    - 连续 3 轮委托率 < 30% 时自动触发自我诊断日志
    - 指标不达标时生成改进建议写入下一轮的 todo
  依赖: forced_delegation_rule, create_parallel_dispatcher
  预估 token 量: 2000

- [ ] 任务ID: parallel_multi_dispatch
  描述: 修改任务派发逻辑，支持同时将多个不同任务派发给不同 Agent，并行执行
  验收标准:
    - 协调者在每轮开始时扫描所有待办任务
    - 将可并行的任务（无依赖关系）同时派发给不同 Agent
    - 例如：同时派任务A给agent-coder、任务B给agent-tester、任务C给agent-reviewer
    - 汇总各 Agent 产出后统一验收 Layer 3
    - 每轮至少并行派发 2 个任务
    - 集成到 parallel_dispatcher.py 中
  依赖: define_layered_delegation, create_parallel_dispatcher
  预估 token 量: 3000

- [ ] 任务ID: cost_incentive_mechanism
  描述: 在成本核算中增加委托激励机制，从成本角度促使协调者委托而非亲力亲为
  验收标准:
    - 在 state.json 或 config.yaml 中增加 DELEGATION_INCENTIVE 配置项：
      coordinator_write_line_threshold: 50   # 协调者自己写超过 50 行时警告
      delegate_success_bonus_tokens: 1000    # 委托成功节省的 token 预算奖励
      self_write_overflow_penalty: 500       # 超过阈值的部分扣减预算
    - 当协调者自己写的代码行数超过阈值时，在日志中输出警告
    - 委托成功率纳入每轮 metrics 指标
    - 集成到 delegate_optimizer.py 的决策逻辑中
  依赖: create_delegate_optimizer
  预估 token 量: 2500

- [x] 任务ID: agent_template_library
  描述: 为每个 Agent 角色创建标准化的 prompt 模板和代码模板，减少因指令不清导致的返工
  验收标准:
    - ✅ templates/ 目录已创建，内含：
      coder_template.md ✓ — 编码任务标准 prompt，含约束条件和示例
      tester_template.md ✓ — 测试任务标准 prompt，含 mock 策略模板和格式要求
      reviewer_template.md ✓ — 审查任务标准 prompt，含 diff 检查清单
    - 每个模板包含：输入格式、输出格式、禁止事项、验证步骤
    - 模板支持参数化（{{variable}} 变量替换）
  依赖: define_agent_roles（已完成 ✅）
  预估 token 量: 2500

- [ ] 任务ID: standardized_layer3_verification
  描述: 将协调者验收Agent产出的流程标准化为4步固定步骤，消除审查随意性
  验收标准:
    - 每次 Agent 产出后，必须执行以下 4 步验收：
      Step 1 — 签名检查：grep 确认函数签名未变化
      Step 2 — 语法检查：python -m py_compile <文件路径>
      Step 3 — 单元测试：pytest <测试文件> -v
      Step 4 — diff 对照：对比改动前后，确认只有预期改动
    - 任何一步失败，记录到 self_evolve_log.json 的 failure_stats，并打回 Agent 重做
    - 同一任务返工超过 2 次，协调者接管并行自己写完
    - 集成到 define_layered_delegation 的 Layer 3 中
  依赖: define_layered_delegation
  预估 token 量: 2000

### Priority: HIGH

- [ ] 任务ID: self_asyncify_knowledge_store
  描述: 将 swarm 项目中假设存在的 services/knowledge_store.py（如果存在）的 sync I/O 函数改为 async def + asyncio.to_thread
  验收标准:
    - 如存在 knowledge_store.py，所有 pymilvus sync 调用加 asyncio.to_thread 包装
    - 保持函数签名完全不变
    - 不改变纯计算函数（保持 sync）
  依赖: 先扫描项目确认文件是否存在
  预估 token 量: 2000

- [ ] 任务ID: self_asyncify_session_service
  描述: 将 swarm 项目中 services/session_service.py 的 create_session 改为 async def
  验收标准:
    - create_session 改为 async def
    - 内部同步 I/O 调用（memory 操作）使用 asyncio.to_thread
    - 通过 tests/test_session.py 全部测试
  依赖: 先扫描项目确认文件是否存在
  预估 token 量: 1500

- [ ] 任务ID: self_container_pool_retry_logic
  描述: 增强容器池启动稳定性——失败重试(3次指数退避)+告警写入日志
  验收标准:
    - 容器启动失败时自动重试 3 次，间隔 1s/2s/4s 指数退避
    - 3 次均失败时写入 logs/container_pool_error.log，格式: [时间戳] FAILED: 失败原因
    - 状态检查失败时同样记录告警日志
    - 在 container_pool.py 或新建 container_pool_retry.py 中实现
  依赖: container_pool.py 存在
  预估 token 量: 1500

- [ ] 任务ID: self_heartbeat_self_healing
  描述: 心跳超时后自动尝试重启失联 agent（通过 PID 文件）
  验收标准:
    - 读取 config.yaml 的 heartbeat_timeout_seconds 和 heartbeat_dir
    - 检测到某 agent 心跳超时（PID 文件存在但文件未更新 > timeout）
    - 自动 kill 原进程后通过 subprocess 重启
    - 记录重启事件到恢复日志（包含时间、agent 名、PID）
    - 单轮最多重启 3 个 agent 防恶性循环
  依赖: self_healing 配置参数（config.yaml 已有）
  预估 token 量: 2000

### Priority: MEDIUM

- [ ] 任务ID: self_cleanup_deprecated_files
  描述: 扫描并删除 swarm 项目中已废弃/无用的文件，更新所有 import 引用
  验收标准:
    - 扫描所有 .py 文件，找出未被任何 import 引用的文件
    - 删除确认无用的文件
    - 更新所有 import 引用
    - 确保删除后所有功能正常运行
  依赖: 无
  预估 token 量: 1000

- [ ] 任务ID: self_cost_tracker_persistence
  描述: 将成本跟踪改为 SQLite 持久化存储，支持跨日累计和成本趋势查询
  验收标准:
    - 创建 cost_tracker.db SQLite 数据库，表结构包含 timestamp/date/cost/provider/model/task_id
    - self_evolve_round.py 的 cost_tracker 逻辑改为读写 SQLite
    - 支持按日期查询历史成本（如: 最近7天趋势）
    - 启动时自动建表，异常降级到内存模式
  依赖: 无
  预估 token 量: 2500

- [ ] 任务ID: self_metrics_sqlite_storage
  描述: 将指标存储从 JSONLines 扩展为 SQLite 原生存储，增加按时间范围聚合查询
  验收标准:
    - 创建 metrics.db SQLite 数据库（与 cost_tracker.db 可合并）
    - swarm_metrics.py 的指标写入逻辑增加 SQLite 适配，兼容原有 JSONLines 写入
    - 支持按时间范围聚合查询（如: 最近24h/7d/30d 的调用量、耗时代码、成功率）
    - 启动时自动建表，异常降级到 JSONLines 文件模式
  依赖: 与 cost_tracker_persistence 共用 SQLite 基础设施
  预估 token 量: 2500

### Priority: LOW

- [ ] 任务ID: self_git_autopush_safety
  描述: 启用 auto_push 前增加分支保护检查，避免误操作覆盖远程代码
  验收标准:
    - 在 self_evolve_round.py 的 git push 逻辑中增加分支检查
    - 检测当前分支是否为 main/master/protected 开头的分支
    - protected 分支上禁止 autopush，输出警告日志并跳过
    - 检查远程是否有未拉取的提交（git fetch + git status ahead/behind 检测）
    - 有 ahead/behind 冲突时输出日志并跳过
  依赖: git shell 命令可用
  预估 token 量: 1500

- [ ] 任务ID: self_json_logs_startup_flag
  描述: 增加 --json-logs 启动参数，生产环境可快速开启 JSON 日志格式
  验收标准:
    - self_evolve_round.py 增加 argparse 参数 --json-logs（默认 False）
    - --json-logs 开启时，logger 的 handler 格式改为 JSON 序列化
    - 保持默认日志格式不变（纯文本），只在显式传参时切换
  依赖: 无
  预估 token 量: 800

---

## 项目三自身维护

- [x] 恢复 cronjob（swarm-evolve-round）
- [x] 启动 tmux daemon（hermes-swarm）
- [x] 配置 round 结束后自动 push 到 GitHub
