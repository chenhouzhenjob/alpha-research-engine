"""验证报告（1c/1d 等）共用的小工具：把池子地址渲染成"可点击的交易对名字"。"""

from __future__ import annotations


def pool_link(pool_address: str, network: str) -> str:
    """GeckoTerminal 公开池子页面，方便人工核对图表；`network` 取 `Chain.value`（如 "bsc"）。"""
    return f"https://www.geckoterminal.com/{network}/pools/{pool_address}"


def pool_label(pool_address: str, pool_names: dict[str, str], network: str) -> str:
    """数据源没有名字时退化成纯地址，不链接。"""
    name = pool_names.get(pool_address)
    if name:
        return f"[{name}]({pool_link(pool_address, network)})"
    return pool_address
