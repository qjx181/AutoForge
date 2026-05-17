#!/usr/bin/env python3
"""swarm_config.py — YAML 驱动的多 Agent 配置管理系统

提供全局配置的加载、验证、运行时更新与序列化能力。
配置优先级（由低到高）: 内置默认值 < config.yaml < .env / 环境变量

用法示例
--------
    from swarm_config import SwarmConfig, load_config

    # 加载配置（自动合并默认值）
    config = load_config("config.yaml")

    # 点分路径访问
    interval = config.get("swarm.round_interval_minutes")

    # 属性访问
    interval = config.swarm.round_interval_minutes

    # 运行时更新
    config.update({"swarm.round_interval_minutes": 60})

    # 序列化
    as_dict = config.to_dict()
"""

import os
import re
import sys
import json
import warnings
from typing import Any, Dict, List, Optional, Union, Tuple

# ── 可选 YAML 依赖 ────────────────────────────────────────────────
try:
    import yaml
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False


# ═══════════════════════════════════════════════════════════════════
# DEFAULT_CONFIG — 完备的模块内置默认配置
# ═══════════════════════════════════════════════════════════════════
DEFAULT_CONFIG: Dict[str, Any] = {
    "swarm": {
        "round_interval_minutes": 30,
        "heartbeat_timeout_seconds": 30,
        "heartbeat_dir": "heartbeats",
        "log_dir": "logs",
        "tmp_dir": "tmp_agent",
    },
    "agents": {
        "enabled": [1, 2, 3, 4, 5, 6, 7, 8],
        "timeout_minutes": 10,
    },
    "git": {
        "auto_push": False,
        "commit_prefix": "swarm-evolve",
    },
}


# ── 类型和范围约束定义 ──────────────────────────────────────────────
# 格式: (type, min, max) — (None, None, None) 表示无范围限制
_VALIDATION_RULES: Dict[str, Tuple[type, Any, Any]] = {
    # swarm 组
    "swarm.round_interval_minutes": (int, 1, None),
    "swarm.heartbeat_timeout_seconds": (int, 1, None),
    "swarm.heartbeat_dir": (str, None, None),
    "swarm.log_dir": (str, None, None),
    "swarm.tmp_dir": (str, None, None),
    # agents 组
    "agents.enabled": (list, None, None),
    "agents.timeout_minutes": (int, 1, None),
    # git 组
    "git.auto_push": (bool, None, None),
    "git.commit_prefix": (str, None, None),
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """_deep_merge — 递归合并两个字典（覆盖策略）。

    作用：将 ``override`` 中的键值递归合并到 ``base`` 中。
    如果某个键对应的值在两个字典中都是 dict，则递归合并；
    否则直接覆盖。

    Args:
        base:     基础字典（将被修改）。
        override: 覆盖字典。

    Returns:
        合并后的字典（与 base 是同一个对象）。
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _get_nested(d: Dict[str, Any], dotted_key: str) -> Any:
    """_get_nested — 通过点分路径从嵌套字典中取值。

    例如: ``_get_nested({"a": {"b": 1}}, "a.b")`` 返回 ``1``。

    Args:
        dotted_key: 点分路径，如 ``"swarm.round_interval_minutes"``。

    Returns:
        路径对应的值。若路径不存在返回 None。
    """
    parts = dotted_key.split(".")
    current: Any = d
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _set_nested(d: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """_set_nested — 通过点分路径设置嵌套字典中的值。

    例如: ``_set_nested(d, "a.b", 1)`` 设置 ``d["a"]["b"] = 1``。

    Args:
        d:          目标字典（会被修改）。
        dotted_key: 点分路径。
        value:      要设置的值。
    """
    parts = dotted_key.split(".")
    current = d
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _dot_flatten(d: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """_dot_flatten — 将嵌套字典拍平为点分键字典。

    例如: ``{"a": {"b": 1}}`` → ``{"a.b": 1}``。

    Args:
        d:      嵌套字典。
        prefix: 递归前缀（内部使用）。

    Returns:
        点分键的扁平字典。
    """
    result: Dict[str, Any] = {}
    for key, value in d.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_dot_flatten(value, dotted))
        else:
            result[dotted] = value
    return result


def _validate_value(key: str, value: Any) -> None:
    """_validate_value — 对单个配置项进行类型和范围校验。

    校验规则定义在 ``_VALIDATION_RULES`` 中。
    校验失败时不抛出异常，仅发出警告并将值回退为默认值。

    Args:
        key:   配置项的点分路径（如 ``"swarm.round_interval_minutes"``）。
        value: 要校验的值。
    """
    if key not in _VALIDATION_RULES:
        return
    expected_type, min_val, max_val = _VALIDATION_RULES[key]

    # 类型检查
    if not isinstance(value, expected_type):
        default_val = _get_nested(DEFAULT_CONFIG, key)
        warnings.warn(
            f"配置项 '{key}' 类型错误: 期望 {expected_type.__name__}, "
            f"得到 {type(value).__name__}('{value}')。已回退到默认值 '{default_val}'。"
        )
        _set_nested(DEFAULT_CONFIG, key, default_val)
        return

    # 范围检查（仅数值类型）
    if expected_type in (int, float):
        if min_val is not None and value < min_val:
            default_val = _get_nested(DEFAULT_CONFIG, key)
            warnings.warn(
                f"配置项 '{key}' 值过小: {value} < {min_val}。"
                f"已回退到默认值 '{default_val}'。"
            )
            _set_nested(DEFAULT_CONFIG, key, default_val)
            return
        if max_val is not None and value > max_val:
            default_val = _get_nested(DEFAULT_CONFIG, key)
            warnings.warn(
                f"配置项 '{key}' 值过大: {value} > {max_val}。"
                f"已回退到默认值 '{default_val}'。"
            )
            _set_nested(DEFAULT_CONFIG, key, default_val)
            return


def _load_env_overrides() -> Dict[str, Any]:
    """_load_env_overrides — 从 .env 文件和系统环境变量加载配置覆盖。

    作用：读取项目根目录下的 ``.env`` 文件（若存在），
    同时扫描系统环境变量中前缀为 ``SWARM_`` 的变量。
    ``.env`` 文件的每一行格式为 ``KEY=VALUE``。
    环境变量的格式为 ``SWARM_SWARM_ROUND_INTERVAL_MINUTES``
    （将大写环境变量名转换为小写点分路径）。

    环境变量优先级：系统环境变量 > .env 文件。

    Returns:
        点分路径 -> 值 的字典（值已做基本类型转换：int/float/bool/str）。
    """
    overrides: Dict[str, Any] = {}

    # ── 尝试读取 .env 文件 ──
    env_file = os.path.join(os.path.dirname(__file__), ".env") if "__file__" in dir() else ".env"
    if os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, raw_val = line.split("=", 1)
                    key = key.strip()
                    raw_val = raw_val.strip().strip("\"'")
                    # 将 SWARM_SWARM_ROUND_INTERVAL_MINUTES 转为 swarm.round_interval_minutes
                    if key.startswith("SWARM_"):
                        dotted = key[len("SWARM_"):].lower().replace("_", ".")
                        overrides[dotted] = _parse_env_value(raw_val)
        except (OSError, IOError):
            pass

    # ── 系统环境变量 ──
    for env_key, env_val in os.environ.items():
        if env_key.startswith("SWARM_"):
            dotted = env_key[len("SWARM_"):].lower().replace("_", ".")
            overrides[dotted] = _parse_env_value(env_val)

    return overrides


def _parse_env_value(raw: str) -> Any:
    """_parse_env_value — 将环境变量字符串值转换为适当类型。

    作用：支持 ``true``/``false`` → bool，数字字符串 → int/float，
    逗号分隔列表 → list，否则保持字符串。

    Args:
        raw: 环境变量值的原始字符串。

    Returns:
        转换后的值。
    """
    if raw.lower() in ("true", "yes", "1"):
        return True
    if raw.lower() in ("false", "no", "0"):
        return False
    if raw.lower() in ("none", "null", ""):
        return None
    # 整数或浮点数
    if re.match(r"^-?\d+$", raw):
        return int(raw)
    if re.match(r"^-?\d+\.\d+$", raw):
        return float(raw)
    # 逗号分隔列表
    if "," in raw:
        items = [item.strip() for item in raw.split(",") if item.strip()]
        # 尝试转换为数字列表
        try:
            return [int(x) if re.match(r"^\d+$", x) else x for x in items]
        except ValueError:
            return items
    return raw


def _load_yaml_file(path: str) -> Optional[Dict[str, Any]]:
    """_load_yaml_file — 从 YAML 文件加载配置。

    若文件不存在、YAML 不可用或解析失败，返回 None。
    文件不存在时不报错（静默返回 None）。

    Args:
        path: YAML 配置文件路径。

    Returns:
        解析后的字典，或 None（文件不存在 / 解析失败）。
    """
    if not os.path.isfile(path):
        return None
    if not _HAS_YAML:
        warnings.warn(
            "PyYAML 未安装。请执行 'pip install pyyaml' 启用 YAML 配置加载。"
            "当前使用内置默认配置。"
        )
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = yaml.safe_load(f)
            return data if isinstance(data, dict) else None
    except (yaml.YAMLError, OSError, IOError) as exc:
        warnings.warn(f"YAML 配置文件 '{path}' 解析失败: {exc}")
        return None


# ── 公共函数 ────────────────────────────────────────────────────────


def load_config(path: str = "config.yaml") -> "SwarmConfig":
    """load_config — 加载配置并返回 SwarmConfig 实例。

    加载顺序（后面的覆盖前面的）:
        1. 模块内置默认值
        2. YAML 配置（文件若存在）
        3. .env 文件和环境变量中的 SWARM_* 覆盖

    若 ``path`` 指定的文件不存在，静默使用默认配置（不报错）。

    Args:
        path: YAML 配置文件路径（默认 ``config.yaml``）。

    Returns:
        完整的 ``SwarmConfig`` 实例。
    """
    return SwarmConfig(path=path)


# ═══════════════════════════════════════════════════════════════════
# SwarmConfig 类
# ═══════════════════════════════════════════════════════════════════
class _ConfigSection:
    """_ConfigSection — 支持属性访问的配置节包装器。

    内部类，将 dict 包装为支持 ``obj.key`` 属性访问的对象。

    Attributes:
        _data: 内部存储的字典数据。
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        """_ConfigSection — 初始化配置节包装器。

        Args:
            data: 字典格式的配置数据。
        """
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        """__getattr__ — 支持 ``section.key`` 属性访问。

        若属性名以 ``_`` 开头或是特殊方法，交由父类处理。
        否则从 ``_data`` 中取值。如果值仍为 dict，则递归包装为
        ``_ConfigSection`` 实例。

        Args:
            name: 属性名。

        Returns:
            属性值或包装后的 _ConfigSection。
        """
        if name.startswith("_") or name in ("to_dict",):
            raise AttributeError(name)
        data = object.__getattribute__(self, "_data")
        if name not in data:
            # 尝试带下划线的变体 (snake_case ↔ 下划线消歧义)
            pass
        if name in data:
            value = data[name]
            if isinstance(value, dict):
                return _ConfigSection(value)
            return value
        raise AttributeError(
            f"配置中不存在属性 '{name}'。可用属性: {list(data.keys())}"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """__setattr__ — 支持 ``section.key = value`` 属性赋值。

        Args:
            name:  属性名。
            value: 要设置的值。
        """
        data = object.__getattribute__(self, "_data")
        data[name] = value

    def __repr__(self) -> str:
        """__repr__ — 返回配置节的可读表示。"""
        data = object.__getattribute__(self, "_data")
        return f"_ConfigSection({data})"

    def to_dict(self) -> Dict[str, Any]:
        """to_dict — 递归将配置节转换为普通字典。

        Returns:
            嵌套的普通字典。
        """
        data = object.__getattribute__(self, "_data")
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, _ConfigSection):
                result[key] = value.to_dict()
            else:
                result[key] = value
        return result


class SwarmConfig:
    """SwarmConfig — Swarm 配置管理系统。

    封装了配置的加载、验证、运行时更新与序列化。

    支持两种访问方式：
    - 点分路径: ``config.get("swarm.round_interval_minutes")``
    - 属性访问: ``config.swarm.round_interval_minutes``

    Attributes:
        config:  存储最终有效配置的嵌套字典。
        _path:   YAML 配置文件路径（字符串）。
        _loaded: 是否成功加载了 YAML 文件（布尔值）。
    """

    def __init__(self, path: str = "config.yaml") -> None:
        """SwarmConfig — 初始化并加载配置。

        加载顺序: 默认值 ← YAML ← .env/环境变量。
        同时执行验证，自动 fallback 不合规的值到默认值并警告。

        Args:
            path: YAML 配置文件路径（默认 ``config.yaml``）。
        """
        self.config: Dict[str, Any] = {}
        self._path: str = path
        self._loaded: bool = False
        self._load()

    # ── 内部方法 ──────────────────────────────────────────────

    def _load(self) -> None:
        """_load — 执行多级配置加载与合并。

        步骤:
            1. 从 DEFAULT_CONFIG 深拷贝初始值
            2. 尝试加载 YAML 文件（若存在则合并）
            3. 应用 .env / 环境变量覆盖
            4. 逐项校验所有配置值
        """
        # 步骤 1：深拷贝默认值
        self.config = json.loads(json.dumps(DEFAULT_CONFIG))

        # 步骤 2：加载 YAML
        yaml_data = _load_yaml_file(self._path)
        if yaml_data is not None:
            _deep_merge(self.config, yaml_data)
            self._loaded = True

        # 步骤 3：环境变量覆盖
        env_overrides = _load_env_overrides()
        for dotted_key, value in env_overrides.items():
            _set_nested(self.config, dotted_key, value)

        # 步骤 4：逐项校验
        flat = _dot_flatten(self.config)
        for dotted_key, value in flat.items():
            _validate_value(dotted_key, value)

    def reload(self) -> None:
        """reload — 重新加载配置文件与环境变量。

        用于在运行时重新读取配置（保留实例，刷新数据）。
        """
        self._load()

    def _recursive_getattr(self, key: str) -> Any:
        """_recursive_getattr — 递归从 config 中查找属性。

        支持 ``swarm.round_interval_minutes`` 等点分路径。

        Args:
            key: 属性名或点分路径。

        Returns:
            对应的值。
        """
        if "." in key:
            current: Any = self.config
            for part in key.split("."):
                if isinstance(current, dict):
                    if part in current:
                        current = current[part]
                    else:
                        raise AttributeError(
                            f"配置路径 '{key}' 不存在（在 '{part}' 处中断）"
                        )
                else:
                    raise AttributeError(
                        f"配置路径 '{key}' 不存在（无法进入非字典节点）"
                    )
            if isinstance(current, dict):
                return _ConfigSection(current)
            return current
        if key not in self.config:
            raise AttributeError(
                f"配置中不存在顶层键 '{key}'。可用键: {list(self.config.keys())}"
            )
        value = self.config[key]
        if isinstance(value, dict):
            return _ConfigSection(value)
        return value

    def __getattr__(self, name: str) -> Any:
        """__getattr__ — 支持 ``config.swarm.round_interval_minutes`` 属性访问。

        若属性名以 ``_`` 开头，交给默认属性查找机制。
        否则遍历 ``config`` 字典。

        Args:
            name: 属性名或点分路径。

        Returns:
            属性值（若为 dict 则包装为 _ConfigSection）。
        """
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        return self._recursive_getattr(name)

    def __setattr__(self, name: str, value: Any) -> None:
        """__setattr__ — 支持 ``config.swarm = {...}`` 顶层属性赋值。

        Args:
            name:  属性名。
            value: 值（若是 dict 则直接替换对应配置节）。
        """
        if name in ("config", "_path", "_loaded"):
            object.__setattr__(self, name, value)
        else:
            self.config[name] = value

    # ── 公共方法 ──────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """get — 通过点分路径获取配置值。

        Args:
            key:     点分路径，如 ``"swarm.round_interval_minutes"``。
            default: 路径不存在时的默认返回值（默认 None）。

        Returns:
            配置值，或 ``default``（路径不存在时）。
        """
        result = _get_nested(self.config, key)
        if result is None:
            return default
        if isinstance(result, dict):
            return _ConfigSection(result)
        return result

    def update(self, updates: Dict[str, Any]) -> None:
        """update — 运行时更新配置项。

        支持两种格式:
        - 点分路径: ``{"swarm.round_interval_minutes": 60}``
        - 嵌套字典: ``{"swarm": {"round_interval_minutes": 60}}``

        更新后自动校验值，不合规则的值回退到默认值并警告。

        Args:
            updates: 要更新的配置映射。
        """
        # 检测是否为点分键
        has_dotted = any("." in k for k in updates)
        if has_dotted:
            for key, value in updates.items():
                if "." in key:
                    _set_nested(self.config, key, value)
                    _validate_value(key, value)
                elif key in self.config:
                    if isinstance(self.config[key], dict) and isinstance(value, dict):
                        _deep_merge(self.config[key], value)
                    else:
                        self.config[key] = value
        else:
            _deep_merge(self.config, updates)
            # 校验所有新设置的值
            flat = _dot_flatten(updates)
            for dotted_key, value in flat.items():
                _validate_value(dotted_key, value)

    def to_dict(self) -> Dict[str, Any]:
        """to_dict — 将配置导出为普通嵌套字典。

        Returns:
            配置的纯字典表示，适合序列化（JSON/YAML）。
        """
        return json.loads(json.dumps(self.config))

    def to_json(self, indent: int = 2) -> str:
        """to_json — 将配置序列化为 JSON 字符串。

        Args:
            indent: 缩进空格数（默认 2）。

        Returns:
            JSON 格式的配置字符串。
        """
        return json.dumps(self.config, ensure_ascii=False, indent=indent)

    def to_yaml(self) -> str:
        """to_yaml — 将配置序列化为 YAML 字符串。

        若 PyYAML 未安装，返回 JSON 格式作为回退。

        Returns:
            YAML（或 JSON）格式的配置字符串。
        """
        if _HAS_YAML:
            return yaml.safe_dump(self.config, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return self.to_json()

    def validate(self) -> List[str]:
        """validate — 全面校验当前配置，返回所有问题列表。

        校验内容：
        - 所有注册到 _VALIDATION_RULES 的项的类型和范围
        - 未注册的配置项仅报告未知（非错误）

        Returns:
            问题描述字符串列表。空列表表示配置完全有效。
        """
        issues: List[str] = []
        flat = _dot_flatten(self.config)

        for dotted_key, value in flat.items():
            if dotted_key not in _VALIDATION_RULES:
                continue
            expected_type, min_val, max_val = _VALIDATION_RULES[dotted_key]

            if not isinstance(value, expected_type):
                issues.append(
                    f"[类型错误] {dotted_key}: "
                    f"期望 {expected_type.__name__}, 得到 {type(value).__name__} ('{value}')"
                )
                continue

            if expected_type in (int, float):
                if min_val is not None and value < min_val:
                    issues.append(
                        f"[范围错误] {dotted_key}: {value} < 允许最小值 {min_val}"
                    )
                if max_val is not None and value > max_val:
                    issues.append(
                        f"[范围错误] {dotted_key}: {value} > 允许最大值 {max_val}"
                    )

        return issues

    def __iter__(self):
        """__iter__ — 支持迭代，产出 (点分键, 值) 对。

        Yields:
            (点分键, 值) 元组。
        """
        flat = _dot_flatten(self.config)
        yield from flat.items()

    def __len__(self) -> int:
        """__len__ — 返回配置项总数（拍平后的键数）。"""
        return len(_dot_flatten(self.config))

    def __contains__(self, key: str) -> bool:
        """__contains__ — 支持 ``"key" in config`` 语法。

        Args:
            key: 点分路径。

        Returns:
            键是否存在。
        """
        return _get_nested(self.config, key) is not None

    def __repr__(self) -> str:
        """__repr__ — 返回配置摘要信息。

        Returns:
            包含路径和加载状态的字符串。
        """
        return (
            f"SwarmConfig(path='{self._path}', loaded_yaml={self._loaded}, "
            f"items={len(self)})"
        )

    def __bool__(self) -> bool:
        """__bool__ — 配置对象始终为 True。"""
        return True


# ═══════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════
def main() -> None:
    """main — CLI 入口函数。

    用法:
        python swarm_config.py               # 打印当前有效配置（YAML，若可用）
        python swarm_config.py --validate    # 验证配置并报告状态
        python swarm_config.py --dump-defaults  # 打印模块内置默认值
        python swarm_config.py --json        # 以 JSON 格式打印配置
        python swarm_config.py -c config.yaml  # 指定配置文件路径
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Swarm Config Manager — YAML 驱动的配置管理系统",
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        default="config.yaml",
        help="YAML 配置文件路径（默认 config.yaml）",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="验证配置并报告状态",
    )
    parser.add_argument(
        "--dump-defaults",
        action="store_true",
        help="打印模块内置默认值",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出（默认 YAML）",
    )
    args = parser.parse_args()

    # ── --dump-defaults 模式 ──
    if args.dump_defaults:
        if _HAS_YAML and not args.json:
            print(yaml.safe_dump(DEFAULT_CONFIG, default_flow_style=False, allow_unicode=True, sort_keys=False))
        else:
            print(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2))
        return

    # ── 正常加载 ──
    config = load_config(args.config)

    # ── --validate 模式 ──
    if args.validate:
        issues = config.validate()
        print("=" * 50)
        print(f"  配置验证报告")
        print(f"  来源: {args.config}")
        print(f"  已加载 YAML: {config._loaded}")
        print(f"  配置项总数: {len(config)}")
        print(f"  问题数: {len(issues)}")
        print("=" * 50)
        if issues:
            for issue in issues:
                print(f"  ❌ {issue}")
        else:
            print("  ✅ 所有配置项均有效")
        print("=" * 50)
        return

    # ── 打印模式 ──
    if _HAS_YAML and not args.json:
        print(yaml.safe_dump(config.config, default_flow_style=False, allow_unicode=True, sort_keys=False))
    else:
        print(config.to_json())


if __name__ == "__main__":
    main()
