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

- [ ] 任务ID: ragas_install_and_integrate
  描述: 安装 ragas + datasets 库，配置 LLM-as-judge 裁判，将 RagasEvaluator 集成到项目一主流程
  验收标准:
    - 项目一环境已安装 ragas 和 datasets（pip install）
    - 配置 LLM 裁判：让 RAGAS 使用 DeepSeek API 或本地 Ollama 做 LLM-as-judge 打分（参考 llm_client.py 的调用方式）
    - 在 evaluation/ 下创建 run_ragas_eval.py 单次运行入口，支持命令行参数（--question / --count）
    - 在 services/ 中建一个 eveluation_service.py（或合并到已有服务），在每次对话结束时异步触发 RagasEvaluator.evaluate_single()
    - 评估结果写入 logs/ragas/ 目录，按日期分文件
    - 报告格式兼容现有的 format_json_report / format_txt_report
    - 运行一次手动测试，输出有效分数（非 0 或纯降级值）
  依赖: build_ragas_evaluator（已存在，基于它做集成）
  预估 token 量: 3500

- [ ] 任务ID: add_stress_test_suite
  描述: 编写压力测试套件，验证系统在 50 并发下的 P95 响应时间 < 10s，成功率 > 95%
  验收标准:
    - 使用 httpx.AsyncClient + asyncio.gather 模拟并发
    - 令牌桶耗尽、Semaphore 槽位占满、缓存穿透 3 种场景独立测试
    - 测试通过条件明确：P95 < 10s, success_rate > 95%
    - 测试结果输出到日志，不阻塞 CI
  依赖: 无
  预估 token 量: 2000

- [ ] 任务ID: asyncify_small_routes
  描述: 将 routes/session.py 和 routes/auth.py 从 sync def 改为 async def
  验收标准:
    - routes/session.py 所有路由改为 async def
    - routes/auth.py 所有路由改为 async def
    - 同步 I/O 调用使用 asyncio.to_thread 或直接 await
    - 通过所有现有测试
  注意: routes/chat.py 已经是 async def，不需要改。
        routes/debug.py 是调试路由，非热路径，保持 sync 即可。
  依赖: 无
  预估 token 量: 2500

- [ ] 任务ID: aggressive_truncation_by_tokens
  描述: 将结果截断从字符数改为按 token 数截断，使用更激进的策略
  验收标准:
    - 使用近似分词（如 tiktoken 或自定义 token 估算）替代字符数切片
    - short_content 截断为 ~300 tokens（约 1200 中文字符的合理上界）
    - excerpt 截断为 ~200 tokens
    - preview 截断为 ~100 tokens
    - 从缓存读取时也按 token 数而非字符数重新截断
  依赖: 无
  预估 token 量: 2000

## Priority: MEDIUM

- [ ] 任务ID: introduce_jieba_tokenizer
  描述: 引入 jieba 分词替换 BM25 的 .split() 空格分词，提升中文召回率
  验收标准:
    - data_loader.py 中 tokenize() 使用 jieba.lcut 替代 .split()
    - 保留英文原样按空格分词，仅中文使用 jieba
    - 对 "你好世界" 等中文测试字符串验证分词结果合理
    - 添加 jieba 到 requirements.txt（如有）
    - 现有检索测试全部通过
  依赖: 无
  预估 token 量: 1500

## Priority: LOW

- [ ] 任务ID: tune_cache_params
  描述: 调优 lru_cache 模块级缓存大小参数（如 cached_encode maxsize=8192）
  验收标准:
    - 分析各缓存的热点数据量，为每个缓存设置匹配的 maxsize
    - 添加注释说明每个缓存大小选择的理由
    - 通过所有现有测试
  依赖: 无
  预估 token 量: 500

---

## 项目三自身维护

- [x] 恢复 cronjob（swarm-evolve-round）
- [x] 启动 tmux daemon（hermes-swarm）
- [ ] 配置 round 结束后自动 push 到 GitHub
