# TODO — 项目三护航任务：持续改进项目一

> 项目三现在的工作目标：用 swarm 自主驱动对项目一的持续改进和优化。
> 项目一目录：`C:\Users\qjx\Desktop\agent-自进化版\项目一cursor版本\在线部分\`

---

## 预处理区（尚未分解的粗略需求）

暂无

---

## Priority: HIGH

- [x] 任务ID: build_ragas_evaluator
  描述: 搭建 RAGAS 评估框架，实现 RagasEvaluator 类，支持 4 项核心指标（context_precision / context_recall / faithfulness / answer_relevancy）
  验收标准:
    - RagasEvaluator 类可独立实例化，传入 retriever + llm_client 即可运行
    - 4 项指标各生成 0~1 浮点分数
    - 当 ragas 库不可用时自动降级（返回占位分数 + 降级日志）
    - 支持输出 JSON 和 TXT 双格式报告
    - 错误处理和超时兜底
  依赖: 无
  预估 token 量: 3000

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

- [ ] 任务ID: add_regression_test_suite
  描述: 搭建回归测试自动化体系，每次修改前后对比核心指标，指标下降超过 5% 自动标记回归
  验收标准:
    - run_tests.sh 或 Makefile 支持 3 种模式：test-only / regression / diff
    - regression 模式下加载基线 JSON，运行测试后对比
    - 任何指标下降 > 5% 时输出 ⛔ REGRESSION DETECTED 并列出具体指标
    - 基线缓存在 tests/regression_baseline/ 目录
  依赖: build_ragas_evaluator（需要其报告格式才能实现对比）
  预估 token 量: 2500

- [ ] 任务ID: add_stress_test_suite
  描述: 编写压力测试套件，验证系统在 50 并发下的 P95 响应时间 < 10s，成功率 > 95%
  验收标准:
    - 使用 httpx.AsyncClient + asyncio.gather 模拟并发
    - 令牌桶耗尽、Semaphore 槽位占满、缓存穿透 3 种场景独立测试
    - 测试通过条件明确：P95 < 10s, success_rate > 95%
    - 测试结果输出到日志，不阻塞 CI
  依赖: 无
  预估 token 量: 2000

- [x] 任务ID: add_rate_limit_tests
  描述: 为 middleware/rate_limit.py 编写单元测试，覆盖 TokenBucket 和 Semaphore 的构造/消耗/恢复/超时
  验收标准:
    - TokenBucket: 构造容量正确 → 消耗不超额 → 恢复速率正确 → 突发消耗后恢复
    - Semaphore: acquire → release → acquire → 超时阻塞 503
    - 使用 asyncio.wait_for 测试 Semaphore 超时
    - 测试覆盖率 > 85%
  依赖: 无
  预估 token 量: 2000

- [x] 任务ID: add_milvus_pool_tests
  描述: 编写 milvus_pool.py 连接池的单元测试
  验收标准:
    - 连接池初始化创建正确数量的连接
    - 连接从池中取出后不可再被分配
    - 连接归还后重新可用
    - 池销毁时正确关闭所有连接
    - Mock Milvus client 实现，不依赖真实 Milvus
  依赖: 无
  预估 token 量: 1500

## Priority: MEDIUM

- [ ] 任务ID: cleanup_duplicate_chat_code
  描述: 清理 chat_service.py 和 chat_pipeline.py 的重复代码，二选一保留
  验收标准:
    - 两个文件对比后，功能完整的保留，另一个标记为 @deprecated
    - 保留的文件需通过所有现有测试
    - main.py 和 routes/chat.py 中的 import 更新为仅使用保留文件
    - 被删除文件仅保留头部 """deprecated""" 注释，其他内容删除
  依赖: 无
  预估 token 量: 1000

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

- [ ] 任务ID: asyncify_session_route
  描述: 将 routes/session.py 从 sync def 改为 async def，使用 httpx.AsyncClient
  验收标准:
    - 所有路由处理函数改为 async def
    - requests.post() 替换为 httpx.AsyncClient
    - 同步 I/O 操作（文件/Redis）使用 asyncio.to_thread 或 aioredis
    - 通过所有现有测试
  依赖: 无
  预估 token 量: 2000

- [ ] 任务ID: asyncify_auth_route
  描述: 将 routes/auth.py 从 sync def 改为 async def，使用 httpx.AsyncClient
  验收标准: （同上）
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
