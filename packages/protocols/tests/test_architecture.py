"""架构约束的静态检查：把"新增协议、新增链不改框架"变成自动化测试（规划 5.1、5.12）。

扫描 `alpha_protocols` 的全部源码（AST），检查：
1. 依赖方向：与链无关的 `decoding/` 不依赖 `decoding/evm/`；框架（`decoding/`、`valuation/`）不依赖任何家族；
   家族之间互不依赖（共用代码放 `families/_shared/`）；
2. 纯度：框架和各家族的 `decoder.py`、`valuation.py` 不依赖存储、网络和链适配器；
3. 不写死链：框架和家族的源码里不出现链名字面量、`Chain.<成员>` 和 40 位十六进制地址
   （零地址除外）。链相关的值只能来自链画像和实例配置。

最后有一组自检，确认检查器本身能抓到违规，避免目录还是空的时候测试"空转通过"。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from alpha_core.types import Chain

PKG_ROOT = Path(__file__).resolve().parents[1] / "src" / "alpha_protocols"
PKG = "alpha_protocols"

# 纯函数模块不能依赖的包：存储、网络、链适配器（需要链上状态时由家族声明，调用方执行）。
IMPURE_PACKAGES = ("alpha_storage", "requests", "web3", "alpha_chains", "httpx", "sqlalchemy")
ZERO_ADDRESS = "0x" + "0" * 40
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
CHAIN_NAMES = {c.value for c in Chain}


@dataclass(frozen=True)
class Violation:
    module: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.module}: [{self.rule}] {self.detail}"


def _module_name(path: Path) -> str:
    rel = path.relative_to(PKG_ROOT).with_suffix("")
    parts = [PKG, *rel.parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(tree: ast.AST, module: str, is_package: bool) -> set[str]:
    """模块 import 的全部绝对模块名（相对导入按所在包解析）。"""
    base = module.split(".") if is_package else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - (node.level - 1)]
                target = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                target = node.module or ""
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _family_of(module: str) -> str | None:
    """`alpha_protocols.families.<family>...` 的家族名；家族目录之外返回 None。"""
    parts = module.split(".")
    if len(parts) >= 3 and parts[1] == "families" and parts[2] not in ("base", "_shared"):
        return parts[2]
    return None


def check_module(module: str, source: str, *, is_package: bool = False) -> list[Violation]:
    """检查一个模块，返回全部违规。"""
    tree = ast.parse(source)
    imports = _imports(tree, module, is_package)
    out: list[Violation] = []

    def bad(rule: str, detail: str) -> None:
        out.append(Violation(module, rule, detail))

    in_decoding = _within(module, f"{PKG}.decoding")
    in_evm = _within(module, f"{PKG}.decoding.evm")
    in_valuation = _within(module, f"{PKG}.valuation")
    family = _family_of(module)
    in_families = _within(module, f"{PKG}.families")
    is_pure = in_decoding or in_valuation or (bool(family) and module.rsplit(".", 1)[-1] in ("decoder", "valuation"))

    for name in sorted(imports):
        if in_decoding and not in_evm and _within(name, f"{PKG}.decoding.evm"):
            bad("chain-agnostic", f"与链无关的 decoding 不能依赖 EVM 专用层：{name}")
        if (in_decoding or in_valuation) and _within(name, f"{PKG}.families"):
            bad("framework-family", f"框架不能依赖家族：{name}")
        other = _family_of(name)
        if family and other and other != family:
            bad("family-family", f"家族 {family} 不能依赖家族 {other}（共用代码放 families/_shared/）：{name}")
        if is_pure and any(_within(name, p) for p in IMPURE_PACKAGES):
            bad("purity", f"纯函数模块不能依赖存储、网络或链适配器：{name}")

    if in_decoding or in_valuation or in_families:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in CHAIN_NAMES:
                    bad("hardcoded-chain", f"出现链名字面量 {node.value!r}（第 {node.lineno} 行）")
                for addr in ADDRESS_RE.findall(node.value):
                    if addr.lower() != ZERO_ADDRESS:
                        bad("hardcoded-address", f"出现地址字面量 {addr}（第 {node.lineno} 行）")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "Chain"
                and node.attr in Chain.__members__
            ):
                bad("hardcoded-chain", f"出现 Chain.{node.attr}（第 {node.lineno} 行）")
    return out


def test_package_obeys_architecture_rules():
    violations: list[Violation] = []
    for path in sorted(PKG_ROOT.rglob("*.py")):
        violations += check_module(_module_name(path), path.read_text(), is_package=path.name == "__init__.py")
    assert not violations, "\n".join(str(v) for v in violations)


# ----------------------------------------------------------------------
# 检查器自检
# ----------------------------------------------------------------------


def _rules(module: str, source: str, *, is_package: bool = False) -> set[str]:
    return {v.rule for v in check_module(module, source, is_package=is_package)}


def test_checker_catches_framework_depending_on_family():
    assert "framework-family" in _rules(f"{PKG}.decoding.dispatch", "from ..families.uniswap_v3_like import x")
    assert "framework-family" in _rules(f"{PKG}.valuation.dispatch", "import alpha_protocols.families.x")


def test_checker_catches_family_depending_on_family_but_allows_shared_and_base():
    mod = f"{PKG}.families.uniswap_v2_like.decoder"
    assert "family-family" in _rules(mod, "from ..uniswap_v3_like.decoder import f")
    assert _rules(mod, "from .._shared import g\nfrom ..base import ProtocolFamily") == set()


def test_checker_catches_chain_agnostic_layer_depending_on_evm():
    assert "chain-agnostic" in _rules(f"{PKG}.decoding.dispatch", "from .evm import flows")
    assert "chain-agnostic" not in _rules(f"{PKG}.decoding.evm.flows", "from . import gas")


def test_checker_catches_impure_imports_only_in_pure_modules():
    assert "purity" in _rules(f"{PKG}.decoding.models", "import requests")
    assert "purity" in _rules(f"{PKG}.families.compound_v2_like.valuation", "from alpha_storage import db")
    # 家族里负责发现合约的模块允许带 I/O（执行由调用方完成，但声明可以引用适配器类型）
    assert "purity" not in _rules(
        f"{PKG}.families.compound_v2_like.discovery", "from alpha_chains.base import ChainAdapter"
    )


def test_checker_catches_hardcoded_chain_and_address():
    src = 'X = "bsc"\nY = Chain.BASE\nZ = "0x' + "ab" * 20 + '"\nOK = "0x' + "00" * 20 + '"\n'
    rules = [v.rule for v in check_module(f"{PKG}.families.wrapped_native.decoder", src)]
    assert rules.count("hardcoded-chain") == 2
    assert rules.count("hardcoded-address") == 1
    # 框架和家族目录之外（例如旧插件）不做这项检查
    assert _rules(f"{PKG}.plugins.pancakeswap_v3", src) == set()
