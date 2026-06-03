<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-Apache%202.0-green" alt="License">
  <img src="https://img.shields.io/badge/Scanner-9%20Dimensions-orange" alt="9 Dimensions">
  <img src="https://img.shields.io/badge/Fixer-6%20Types-purple" alt="6 Fixers">
  <img src="https://img.shields.io/badge/Self--Evolution-✅-brightgreen" alt="Self Evolution">
</p>

<h1 align="center">AutoForge</h1>
<p align="center"><b>自进化代码质量引擎 — AI Agent 自动扫描、修复、学习，越用越聪明</b></p>
<p align="center">
  一个能持续运行的多 Agent 系统：扫描你的代码 → 自动修复 → 验证结果 → 积累经验 → 下次修得更好<br>
  不是静态的 linter，不是一次性的 formatter，是一个<strong>会成长的代码管家</strong>。
</p>

---

## 🎯 为什么需要它？

| 痛点 | AutoForge 的解法 |
|------|-----------------|
| Linter 只报问题不修 | **9 维度扫描 + 6 类自动修复**，发现问题直接修 |
| 修完不知道改对没改对 | **成功验证器**：自动跑语法检查、测试、import 链验证 |
| 每次从零开始，不长记性 | **经验闭环**：成功修复积累经验，失败修复学习教训，下次更准 |
| 修了不该修的 | **置信度门控**：高置信自动应用，中置信等人工审批，低置信只记录 |
| 成本失控 | **三级熔断**：$5/天预算，超限自动降级，超 2x 紧急刹车 |

---

## 🏗️ 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    AutoForge 自进化引擎                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │  Scanner  │───▶│  Fixer   │───▶│  Gate    │───▶│  Learner │  │
│  │  Registry │    │ Registry │    │ 置信度   │    │ 经验积累 │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│       │               │               │               │        │
│  9 个维度扫描    6 类修复器      三级决策         记录→校准      │
│  安全/性能/      异常吞没/       ≥0.8 自动        →注入→建议    │
│  质量/测试/      裸except/       0.5~0.8 待审      创建skill    │
│  架构/文档/      print滥用/      <0.5 拒绝                       │
│  配置/死代码     资源泄漏/                                       │
│                 缺超时/缺返回类型                                │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                  基础设施层 (Infra)                       │  │
│  │  SQLite 成本追踪 │ 审计日志 │ 安全锁 │ Prometheus 指标    │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**核心流程：扫描 → 修复 → 门控 → 验证 → 学习**

每一轮自进化，系统经历五个阶段：
1. **扫描**：9 个维度扫描器并行扫描目标项目，产出 Issue 列表
2. **修复**：根据 Issue 类型匹配修复器，生成修复方案
3. **门控**：置信度 ≥ 0.8 自动应用，0.5~0.8 进审批队列，< 0.5 拒绝
4. **验证**：成功验证器检查修复是否真正有效（语法、测试、import）
5. **学习**：成功经验积累到经验库，失败教训记录到学习模块

---

## 🔍 9 维度扫描器

| 维度 | 扫描器 | 检测内容 |
|------|--------|---------|
| 🔒 安全 | `sec_scanner` | SQL 注入、硬编码密钥、不安全的 pickle/yaml 加载 |
| ⚡ 性能 | `perf_scanner` | 循环内重复计算、N+1 查询、不必要的大对象复制 |
| 🔄 异步化 | `async_scanner` | 可异步化的同步 I/O、事件循环阻塞 |
| 📊 代码质量 | `quality_scanner` | 深层嵌套、过长函数、复杂条件、Magic Number |
| 🧪 测试覆盖 | `test_scanner` | 未测试的公开函数、缺少边界测试 |
| 🏛️ 架构 | `arch_scanner` | 循环依赖、上帝类、违反 SOLID 原则 |
| 📝 文档 | `doc_scanner` | 缺少 docstring、过时注释、无类型注解 |
| ⚙️ 配置 | `config_scanner` | 硬编码 IP/端口/路径、配置项分散 |
| 💀 死代码 | `deadcode_scanner` | 未使用的 import、死函数、重复文件 |

---

## 🔧 6 类自动修复器

| 修复器 | 修复类型 | 示例 |
|--------|---------|------|
| `swallowed_exception` | 异常吞没 | `except: pass` → `except Exception as e: logger.warning(...)` |
| `bare_except` | 裸 except | `except:` → `except Exception:` |
| `print_used` | print 滥用 | `print(...)` → `logger.info(...)` |
| `resource_not_managed` | 资源泄漏 | `open()` → `with open() as f:` |
| `missing_timeout_config` | 缺超时配置 | `requests.get(url)` → `requests.get(url, timeout=30)` |
| `missing_return_type` | 缺返回类型 | `def foo():` → `def foo() -> str:` |

---

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/qjx181/AutoForge.git
cd AutoForge
pip install -r requirements.txt
```

### 一行命令扫描任意项目

```bash
python moreagent.py scan /path/to/your/project
```

### 注册优化目标（自动循环）

```bash
python moreagent.py setup /path/to/your/project
python moreagent.py cron on    # 开启每 2 小时自动扫描
```

### 查看状态和成本

```bash
python moreagent.py status     # 系统状态
python moreagent.py cost       # 成本报告
python moreagent.py history    # 历史修复记录
```

### 生成 CI 配置

```bash
python moreagent.py init-ci /path/to/your/project
```

---

## 🧠 自进化机制

AutoForge 的核心差异化：**它不只是执行规则，它会学习。**

```
                  ┌─────────────────────────────────┐
                  │        经验闭环 (Learning)       │
                  ├─────────────────────────────────┤
                  │                                 │
                  │  成功修复 ──▶ 提取模式          │
                  │      │           │              │
                  │      ▼           ▼              │
                  │  记录经验     校准置信度         │
                  │      │           │              │
                  │      ▼           ▼              │
                  │  下次注入     动态调整阈值       │
                  │  相关经验                        │
                  │      │                          │
                  │      ▼                          │
                  │  3 次成功 ──▶ 自动建议创建 Skill │
                  │                                 │
                  │  失败修复 ──▶ 记录教训          │
                  │      │                          │
                  │      ▼                          │
                  │  同类问题不再重蹈覆辙            │
                  └─────────────────────────────────┘
```

- **置信度动态校准**：`calibrated = original × √(success_rate)`，修复器用得越多，置信度越准
- **经验注入**：修复前自动检索相关经验，作为上下文注入修复器
- **失败学习**：和 `evolve_learn.py` 互补，成功积累 + 失败教训 = 完整学习闭环
- **自动 Skill 生成**：某个修复模式成功 3 次以上，自动建议创建可复用的 Hermes Skill

---

## 🛡️ 安全设计

| 机制 | 说明 |
|------|------|
| **置信度门控** | 三级决策：AUTO_APPLY / PENDING_REVIEW / REJECTED |
| **安全锁** | Deny-by-default：删除文件、git push 等危险操作需二次确认 |
| **审计日志** | 所有拦截/拒绝/确认事件写入 `logs/audit.jsonl` |
| **成本熔断** | $5/天预算，超限降级，超 2x 紧急刹车 |
| **回退机制** | 新管道异常时自动回退到旧版扫描逻辑 |

---

## 📁 项目结构

```
AutoForge/
├── moreagent.py                   # CLI 入口
├── config.yaml                    # 全局配置
├── src/
│   ├── core/                      # 核心管道
│   │   ├── adapters_pkg/          # Scanner/Fixer 标准化接口
│   │   ├── fix_pipeline.py        # 5 阶段集成管道
│   │   ├── confidence_gate.py     # 置信度门控
│   │   ├── experience_store.py    # 经验积累闭环
│   │   ├── success_verifier.py    # 修复验证器
│   │   └── incremental_scanner.py # 增量扫描（git diff/mtime）
│   ├── analysis/                  # 扫描器
│   │   ├── dims/                  # 9 维度扫描器
│   │   └── reviewers/             # 5 类审查器
│   ├── fixers/                    # 修复器
│   ├── agents/                    # Agent 调度
│   ├── infra/                     # 基础设施
│   └── api/                       # REST API
├── data/                          # 运行时数据
├── logs/                          # 日志 + 审计
└── tests/                         # 测试
```

---

## 🔗 同类项目对比

| 特性 | AutoForge | Ruff | SonarQubit | CodeRabbit |
|------|-----------|------|------------|------------|
| 自动修复 | ✅ 6 类 | ✅ 部分 | ❌ 只报告 | ❌ 只建议 |
| 自我学习 | ✅ 经验闭环 | ❌ | ❌ | ❌ |
| 置信度门控 | ✅ 三级 | ❌ | ❌ | ❌ |
| 多维度扫描 | ✅ 9 维度 | ⚠️ lint only | ✅ 多维度 | ⚠️ PR review |
| 成本控制 | ✅ 三级熔断 | N/A | 💰 商业 | 💰 商业 |
| 增量扫描 | ✅ git diff | ✅ | ✅ | ✅ |
| 自定义修复器 | ✅ 插件式 | ⚠️ 有限 | ✅ | ❌ |
| 开源 | ✅ Apache 2.0 | ✅ MIT | ⚠️ Community | ❌ |

---

## 🤝 Contributing

欢迎贡献新的扫描维度和修复器！

### 添加新的扫描维度

```python
# src/analysis/dims/my_scanner.py
def scan(blueprint: dict) -> dict:
    """扫描 blueprint 中的文件，返回 issue 列表"""
    issues = []
    # ... 你的扫描逻辑
    return {"dimension": "my_dimension", "issues": issues}
```

### 添加新的修复器

```python
# src/fixers/my_fixer.py
def try_fix(issue: dict, file_content: str) -> dict:
    """尝试修复 issue，返回修复结果"""
    # ... 你的修复逻辑
    return {"success": True, "fixed_content": "...", "confidence": 0.9}
```

---

## 📄 License

[Apache License 2.0](LICENSE)

---

<p align="center">
  <b>不是静态工具，是会成长的代码管家。</b><br>
  <sub>Made by <a href="https://github.com/qjx181">qjx181</a></sub>
</p>
