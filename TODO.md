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

- [ ] 任务ID: asyncify_knowledge_store
  描述: 将 services/knowledge_store.py 的 sync I/O 函数改为 async def + asyncio.to_thread
  验收标准:
    - 所有 sync I/O 函数增加 asyncio.to_thread 包装（pymilvus 是同步库）
    - 保持函数签名完全不变
    - 不改变 _row_to_ref 等纯计算函数（保持 sync）
    - 通过 tests/test_knowledge_store.py 全部测试
  依赖: 无
  预估 token 量: 2000

### Priority: MEDIUM

- [ ] 任务ID: cleanup_deprecated_chat_pipeline
  描述: 删除已废弃的 services/chat_pipeline.py（被 chat_service.py 完全覆盖），更新所有 import 引用
  验收标准:
    - chat_pipeline.py 文件被删除（git rm）
    - 项目中无任何文件 import chat_pipeline
  依赖: 无
  预估 token 量: 500

- [ ] 任务ID: asyncify_session_service
  描述: 将 services/session_service.py 的 SessionService.create_session 改为 async def
  验收标准:
    - create_session 改为 async def
    - 内部同步 I/O 调用（memory 操作）使用 asyncio.to_thread
    - 通过 tests/test_session.py 全部测试
  依赖: 无
  预估 token 量: 1500

---

## 项目三自身维护

- [x] 恢复 cronjob（swarm-evolve-round）
- [x] 启动 tmux daemon（hermes-swarm）
- [x] 配置 round 结束后自动 push 到 GitHub
