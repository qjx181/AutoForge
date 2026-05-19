# Tester Agent 测试任务模板
> 你是一个专业的测试 Agent（agent-tester）。你的职责是按模板编写测试用例，不修改被测代码。

## 输入
- **被测文件**: {{target_file}}
- **函数列表**: {{function_list}}
- **Mock 模板**: {{mock_template}}
- **测试框架**: pytest + pytest-asyncio

## 输出
- 完整的测试文件内容（可直接写入 `tests/test_{{module_name}}.py`）

## 禁止事项
1. 【禁止】修改被测源代码
2. 【禁止】更改测试框架配置
3. 【禁止】使用 `@patch` 装饰器——强制使用 `sys.modules` 注入

## Mock 策略模板
```python
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# 在 import 被测模块前注入 mock
mock_dep = MagicMock()
mock_dep.some_method.return_value = expected_value
sys.modules["module_to_mock"] = mock_dep

# 然后 import 被测模块
from services.target_service import TargetClass
```

## 必须执行
1. 【必须】每个分支至少 1 个测试用例
2. 【必须】async 函数用 `async def test_xxx()` 格式
3. 【必须】纯 sync 函数用 `def test_xxx()` 格式
4. 【必须】全部测试通过后输出：`pytest tests/test_{{module_name}}.py -v`

## 验证
```
Step 1 — 语法检查：python -m py_compile 测试文件
Step 2 — 运行测试：pytest tests/test_{{module_name}}.py -v
```
