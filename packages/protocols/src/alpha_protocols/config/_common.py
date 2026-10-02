"""配置加载的共用工具。"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import AfterValidator

_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


def check_address(value: str) -> str:
    """地址必须是小写、带 0x 的 40 位十六进制。要求写成小写，避免同一地址两种写法在去重时漏掉。"""
    if not _ADDRESS.match(value):
        raise ValueError(f"地址必须是小写、带 0x 的 40 位十六进制：{value!r}")
    return value


# 配置文件里的 EVM 地址
Address = Annotated[str, AfterValidator(check_address)]


class ConfigError(ValueError):
    """配置文件有误：格式不对、引用了不存在的链或家族、地址冲突等。消息里带上文件名。"""


def package_dir(name: str) -> Path:
    """`alpha_protocols` 包内的数据目录（chains、instances）。"""
    return Path(str(resources.files("alpha_protocols") / name))


def read_yaml(path: Path) -> dict[str, Any]:
    """读取一份 YAML 配置；顶层必须是映射。

    @raises ConfigError 文件不是合法的 YAML 映射
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}：YAML 格式错误：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}：顶层必须是映射")
    return data
