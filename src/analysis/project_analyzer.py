"""project_analyzer.py — 项目结构自动检测

作用：
  分析任意项目目录，识别语言、框架、包管理器、测试框架、
  代码文件分布，输出"该扫什么、怎么扫"的蓝图。

工作原理：
  1. 扫描文件类型分布 → 识别语言
  2. 扫描配置文件 → 识别框架 + 包管理器
  3. 扫描目录结构 → 识别源码/测试/文档区域
  4. 汇总为 OptimizationBlueprint 供下游各维度扫描器使用

用法：
    from project_analyzer import analyze_project, get_optimization_blueprint
    blueprint = analyze_project("/path/to/project")
"""

import os
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional




@dataclass
class LanguageProfile:
    """编程语言画像"""
    primary: str                       # 主要语言
    all: list[str]                     # 所有检测到的语言
    file_counts: dict[str, int]       # 各语言文件数
    total_files: int                   # 代码文件总数
    frameworks: list[str]              # 检测到的框架
    package_managers: list[str]       # 检测到的包管理器


@dataclass
class TestProfile:
    """测试画像"""
    has_tests: bool
    test_framework: str                # pytest / unittest / jest / ...
    test_dir: Optional[str]            # 测试目录路径
    test_file_pattern: str             # glob 模式
    coverage_tool: Optional[str]       # coverage / jest / ...
    coverage_file: Optional[str]       # coverage 文件路径


@dataclass
class StructureProfile:
    """目录结构画像"""
    source_dirs: list[str]
    docs_dirs: list[str]
    config_files: list[str]
    build_files: list[str]
    ci_files: list[str]
    all_source_files: list[str]        # 所有源代码文件（绝对路径）


@dataclass
class OptimizationBlueprint:
    """优化蓝图——告诉下游扫描器"扫什么、怎么扫"

    所有路径均为绝对路径。
    """
    project_root: Path
    project_name: str
    language: LanguageProfile
    test: TestProfile
    structure: StructureProfile

    enabled_dimensions: dict[str, bool] = field(default_factory=dict)

    def is_enabled(self, dimension: str) -> bool:
        return self.enabled_dimensions.get(dimension, False)

    def get_source_files(self, language: str = "python") -> list[str]:
        """获取指定语言的所有源代码文件"""
        return [
            f for f in self.structure.all_source_files
            if f.endswith(self._ext_for_language(language))
        ]

    def _ext_for_language(self, lang: str) -> str:
        return {
            "python": ".py",
            "javascript": ".js",
            "typescript": ".ts",
            "java": ".java",
            "go": ".go",
            "rust": ".rs",
        }.get(lang, "")




LANGUAGE_SIGNATURES: dict[str, dict] = {
    "python": {
        "files": [".py"],
        "markers": ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile"],
        "dirs": ["__pycache__", ".venv", "venv"],
        "configs": ["pytest.ini", "setup.cfg", "tox.ini"],
    },
    "javascript": {
        "files": [".js", ".jsx", ".mjs"],
        "markers": ["package.json"],
        "dirs": ["node_modules"],
        "configs": ["jest.config.js", "webpack.config.js", "vite.config.js"],
    },
    "typescript": {
        "files": [".ts", ".tsx"],
        "markers": ["tsconfig.json"],
        "dirs": [],
        "configs": ["tsconfig.json"],
    },
    "java": {
        "files": [".java"],
        "markers": ["pom.xml", "build.gradle"],
        "dirs": ["target", "build"],
        "configs": ["pom.xml", "build.gradle"],
    },
    "go": {
        "files": [".go"],
        "markers": ["go.mod"],
        "dirs": [],
        "configs": ["go.mod", "go.sum"],
    },
    "rust": {
        "files": [".rs"],
        "markers": ["Cargo.toml"],
        "dirs": ["target"],
        "configs": ["Cargo.toml"],
    },
}


TEST_FRAMEWORKS: dict[str, dict] = {
    "python": {
        "pytest": ["pytest.ini", "setup.cfg", "conftest.py"],
        "unittest": [],
    },
    "javascript": {
        "jest": ["jest.config.js", "package.json"],
        "mocha": ["mocha.opts"],
        "vitest": ["vitest.config.ts", "vitest.config.js"],
    },
    "java": {
        "junit": ["pom.xml", "build.gradle"],
    },
    "go": {
        "testing": ["go.mod"],
    },
    "rust": {
        "rust": ["Cargo.toml"],
    },
}


def _scan_tree(root: Path, max_files: int = 5000) -> dict[str, int]:
    """统计文件类型分布。"""
    counts: dict[str, int] = {}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not d.startswith(".")
                       and d not in ("node_modules", "__pycache__", ".venv", "venv",
                                     "target", "dist", "build", ".git", "vendor",
                                     "__pypackages__")])
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
                       ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
                       ".kt"):
                counts[ext] = counts.get(ext, 0) + 1
                if len(found) < max_files:
                    found.append(os.path.join(dirpath, filename))
    return counts, found


def _detect_frameworks(root: Path, lang: str) -> list[str]:
    """检测框架。"""
    frameworks = []
    markers = LANGUAGE_SIGNATURES.get(lang, {}).get("markers", [])
    configs = LANGUAGE_SIGNATURES.get(lang, {}).get("configs", [])

    py_files = {f.name for f in root.rglob("*.py") if f.is_file()}

    if lang == "python":
        if "app.py" in py_files or any("app = FastAPI" in f.read_text(errors="ignore") for f in root.rglob("*.py") if f.is_file()):
            frameworks.append("fastapi")
        if any("Flask" in f.read_text(errors="ignore") for f in root.rglob("*.py") if f.is_file()):
            frameworks.append("flask")
        if any("Django" in f.read_text(errors="ignore") for f in root.rglob("*.py") if f.is_file()):
            frameworks.append("django")
        if any("uvicorn" in f.read_text(errors="ignore") for f in root.rglob("*.py") if f.is_file()):
            frameworks.append("uvicorn")
        if any("async def" in f.read_text(errors="ignore") for f in root.rglob("*.py") if f.is_file()):
            frameworks.append("asyncio")

    elif lang == "javascript":
        pkg = root / "package.json"
        if pkg.exists():
            text = pkg.read_text(errors="ignore")
            for fw in ("react", "vue", "next", "nuxt", "express", "nest"):
                if fw in text.lower():
                    frameworks.append(fw)

    elif lang == "java":
        if (root / "pom.xml").exists():
            text = (root / "pom.xml").read_text(errors="ignore")
            if "spring-boot" in text:
                frameworks.append("spring-boot")
            if "junit" in text:
                frameworks.append("junit")
        if (root / "build.gradle").exists():
            text = (root / "build.gradle").read_text(errors="ignore")
            if "spring" in text:
                frameworks.append("spring-boot")

    return frameworks


def _detect_package_managers(root: Path) -> list[str]:
    """检测包管理器。"""
    pm = []
    markers = [
        ("pip", ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile.lock", "poetry.lock"]),
        ("npm", ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]),
        ("maven", ["pom.xml"]),
        ("gradle", ["build.gradle", "build.gradle.kts"]),
        ("go", ["go.mod"]),
        ("cargo", ["Cargo.lock"]),
        ("dotnet", ["*.csproj"]),
    ]
    for name, files in markers:
        if any((root / f).exists() for f in files):
            pm.append(name)
    return pm


def _detect_test_profile(root: Path, lang: str) -> TestProfile:
    """检测测试框架和配置。"""
    if lang not in TEST_FRAMEWORKS:
        return TestProfile(False, "none", None, "", None, None)

    test_dir_patterns = {
        "python": ["tests", "test", "testing"],
        "javascript": ["tests", "test", "__tests__"],
        "java": ["src/test"],
        "go": [],
        "rust": ["tests"],
    }
    test_dirs = test_dir_patterns.get(lang, [])

    found_test_dir = None
    for td in test_dirs:
        if (root / td).is_dir():
            found_test_dir = str(root / td)
            break

    framework_info = TEST_FRAMEWORKS[lang]
    found_framework = "unittest" if lang == "python" else "none"

    if "pytest" in framework_info:
        for marker in framework_info["pytest"]:
            if (root / marker).exists():
                found_framework = "pytest"
                break
        if found_test_dir:
            for f in Path(found_test_dir).rglob("conftest.py"):
                found_framework = "pytest"
                break

    elif "jest" in framework_info:
        if (root / "jest.config.js").exists() or (root / "jest.config.ts").exists():
            found_framework = "jest"
        pkg = root / "package.json"
        if pkg.exists() and "jest" in pkg.read_text(errors="ignore"):
            found_framework = "jest"

    coverage_tool = None
    coverage_file = None
    for cf in ["coverage/", ".coverage", "htmlcov/", "lcov.info", "coverage.xml"]:
        if (root / cf).exists() or any((root / cf2).exists() for cf2 in [cf]):
            if "coverage" in cf or cf == ".coverage":
                coverage_tool = "coverage"
                coverage_file = str(root / cf)
                break

    return TestProfile(
        has_tests=found_test_dir is not None,
        test_framework=found_framework,
        test_dir=found_test_dir,
        test_file_pattern="*" if lang == "python" else "*",
        coverage_tool=coverage_tool,
        coverage_file=coverage_file,
    )


def _detect_structure(root: Path) -> StructureProfile:
    """检测目录结构。"""
    source_dirs, docs_dirs = [], []
    config_files, build_files, ci_files = [], [], []

    for item in root.iterdir():
        if not item.is_dir() or item.name.startswith("."):
            continue
        low = item.name.lower()
        if low in ("src", "lib", "core", "internal", "app", "apps"):
            source_dirs.append(str(item))
        elif low in ("docs", "doc", "documentation"):
            docs_dirs.append(str(item))

    for item in root.iterdir():
        if not item.is_file():
            continue
        low = item.name.lower()
        if low in ("dockerfile", "docker-compose.yml", "docker-compose.yaml",
                   "makefile", "cmakelists.txt", "build.sh", "tox.ini",
                   "setup.py", "pyproject.toml", "requirements.txt",
                   "package.json", "go.mod", "cargo.toml", "pom.xml"):
            build_files.append(str(item))
        elif low in (".env.example", ".env.template", "config.yaml", "config.yml",
                     "settings.py", "settings.json", "config.toml"):
            config_files.append(str(item))
        elif low.startswith(".github") or low in ("azure-pipelines.yml", ".gitlab-ci.yml", "Jenkinsfile", "circleci"):
            ci_files.append(str(item))

    return StructureProfile(
        source_dirs=source_dirs or [str(root)],
        docs_dirs=docs_dirs,
        config_files=config_files,
        build_files=build_files,
        ci_files=ci_files,
        all_source_files=[],
    )


def _infer_enabled_dimensions(lang: str, language: LanguageProfile,
                               test: TestProfile,
                               structure: StructureProfile) -> dict[str, bool]:
    """根据项目结构推断哪些优化维度应该启用。"""
    enabled = {
        "quality": language.total_files > 0,
        "testing": test.has_tests,
        "performance": language.primary in ("python", "javascript", "java", "typescript"),
        "architecture": language.total_files >= 5,
        "security": language.primary in ("python", "javascript", "typescript", "java"),
        "documentation": bool(structure.docs_dirs) or (root / "README.md").exists() if root else False,
        "configuration": bool(structure.config_files),
        "asyncification": "asyncio" in language.frameworks or language.primary in ("python", "javascript", "typescript"),
        "deadcode": language.total_files >= 3,
    }
    return enabled


def analyze_project(path: str) -> OptimizationBlueprint:
    """分析项目目录，返回完整的优化蓝图。

    Args:
        path: 项目根目录路径（可以是相对路径）

    Returns:
        OptimizationBlueprint: 包含语言/测试/结构/维度开关的完整蓝图
    """
    global root
    root = Path(path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"项目目录不存在: {root}")

    project_name = root.name

    file_counts, all_source_files = _scan_tree(root)

    ext_to_lang = {
        ".py": "python", ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".java": "java",
        ".go": "go", ".rs": "rust",
    }
    lang_counts: dict[str, int] = {}
    for ext, count in file_counts.items():
        lang = ext_to_lang.get(ext, "other")
        lang_counts[lang] = lang_counts.get(lang, 0) + count

    sorted_langs = sorted(lang_counts.items(), key=lambda x: -x[1])
    primary = sorted_langs[0][0] if sorted_langs else "unknown"
    all_langs = [l for l, _ in sorted_langs if l != "other"]

    frameworks = _detect_frameworks(root, primary)

    pms = _detect_package_managers(root)

    language = LanguageProfile(
        primary=primary,
        all=all_langs,
        file_counts=lang_counts,
        total_files=sum(lang_counts.values()),
        frameworks=frameworks,
        package_managers=pms,
    )

    test = _detect_test_profile(root, primary)

    structure = _detect_structure(root)
    structure.all_source_files = all_source_files

    enabled = _infer_enabled_dimensions(primary, language, test, structure)

    return OptimizationBlueprint(
        project_root=root,
        project_name=project_name,
        language=language,
        test=test,
        structure=structure,
        enabled_dimensions=enabled,
    )




def get_optimization_blueprint(path: str) -> OptimizationBlueprint:
    """analyze_project 的别名。"""
    return analyze_project(path)


def print_blueprint(bp: OptimizationBlueprint) -> str:
    """将蓝图输出为人类可读的摘要文本。"""
    lines = [
        f"=== 项目分析: {bp.project_name} ===",
        f"根目录: {bp.project_root}",
        f"主要语言: {bp.language.primary} ({bp.language.total_files} 个代码文件)",
    ]
    if bp.language.all:
        lines.append(f"所有语言: {', '.join(bp.language.all)}")
    if bp.language.frameworks:
        lines.append(f"框架: {', '.join(bp.language.frameworks)}")
    if bp.language.package_managers:
        lines.append(f"包管理器: {', '.join(bp.language.package_managers)}")

    lines.append(f"\n测试框架: {bp.test.test_framework} (目录: {bp.test.test_dir or '未检测到'})")

    lines.append(f"\n启用优化维度:")
    for dim, enabled in bp.enabled_dimensions.items():
        icon = "[ON ]" if enabled else "[off ]"
        lines.append(f"  {icon} {dim}")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python project_analyzer.py <项目目录>")
        sys.exit(1)
    bp = analyze_project(sys.argv[1])
    print(print_blueprint(bp))

