"""YAML 配置加载。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def load_yaml(path: str | Path) -> dict[str, Any]:
    """读取 YAML 为 dict。"""
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise TypeError(f"配置必须是 mapping: {path}")
    return data


def default_root() -> Path:
    """仓库根目录（含 configs/）。"""
    return Path(__file__).resolve().parents[2]


def ensure_dotenv() -> None:
    """加载仓库根目录 .env（若存在）；不覆盖已有环境变量。"""
    load_dotenv(default_root() / ".env", override=False)


def load_collect(path: str | Path | None = None) -> dict[str, Any]:
    """加载 collect.yaml。"""
    ensure_dotenv()
    p = Path(path) if path else default_root() / "configs" / "collect.yaml"
    return load_yaml(p)


def load_source(source: str, path: str | Path | None = None) -> dict[str, Any]:
    """加载 source 配置；Binance 的不同市场 profile 共用一个配置文件。"""
    ensure_dotenv()
    if path:
        return load_yaml(path)
    if source in {"binance_perp", "binance_spot"}:
        shared = load_yaml(default_root() / "configs" / "collection" / "sources" / "binance.yaml")
        market_type = source.removeprefix("binance_")
        markets = shared.pop("markets", {})
        if not isinstance(markets, dict) or market_type not in markets:
            raise ValueError(f"binance.yaml 缺少市场 profile: {market_type}")
        profile = markets[market_type]
        if not isinstance(profile, dict):
            raise ValueError(f"binance.yaml 的 {market_type} profile 须为 mapping")
        return {**shared, **profile, "market_type": market_type}
    return load_yaml(default_root() / "configs" / "collection" / "sources" / f"{source}.yaml")
