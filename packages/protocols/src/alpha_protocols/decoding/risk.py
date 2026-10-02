"""token 风险标记（纯函数，规划 5.9）。

风险标记只改变事件的子类型（`receive/spam`）和 M3 是否把它计入资金，不删除任何事件：
被误判的代价是一条事件的子类型错了，可以人工修正；漏记事件的代价是资金对不上（设计文档 G3）。

判断依据只有 symbol、name 和地址，不看金额。基础资产清单（每条链的 USDT、USDC、WBNB……的
真实地址）来自链画像，调用方传入；地址在清单里的 token 一律是 normal。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from .models import RiskFlag, TokenMeta

# 常见同形字符 → 拉丁字母。NFKC 已经处理了全角字符；这里补上 NFKC 不会折叠的西里尔、希腊字母，
# 以及仿冒 token 常用的数字和符号替代（样本里的 `U5DT`、`UЅDТ`）。
_CONFUSABLES = str.maketrans(
    {
        # 西里尔字母
        "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J", "К": "K", "М": "M",
        "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "Х": "X", "У": "Y", "Ү": "Y", "Ԁ": "D", "Ս": "U",
        "а": "A", "с": "C", "е": "E", "і": "I", "ј": "J", "о": "O", "р": "P", "ѕ": "S", "х": "X", "у": "Y",
        # 希腊字母
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
        "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "O", "υ": "U", "τ": "T",
        # 切罗基字母
        "Ꭰ": "D", "Ꮪ": "S", "Ꭲ": "T", "Ꮯ": "C", "Ꭼ": "E",
        # 数字和符号
        "0": "O", "1": "I", "5": "S", "$": "S", "€": "E",
    }
)  # fmt: skip

# 零宽字符和各种空白：仿冒 token 常在 symbol 前后塞 U+200A（细空格）之类的字符
_INVISIBLE = re.compile(r"[\s\u200b-\u200f\u2028-\u202f\u205f-\u206f\ufeff]")

# 垃圾空投的特征：名称或 symbol 里带网址（诱导用户去钓鱼网站"领取"）。只认网址类特征，
# 不认 claim、reward 这类单词：正常项目名里也常见，误判代价不值得。
_SPAM_PATTERN = re.compile(
    r"(https?://|www\.|t\.me/|\.(com|io|us|org|net|xyz|app|gift|site|top|vip|cc|pro|fun|finance)\b)",
    re.IGNORECASE,
)


def normalize_symbol(symbol: str) -> str:
    """把 symbol 规范化到可比较的形式：NFKC → 去掉不可见字符 → 同形字符映射到拉丁字母 → 大写。"""
    text = unicodedata.normalize("NFKC", symbol)
    text = _INVISIBLE.sub("", text)
    return text.translate(_CONFUSABLES).upper()


def classify_token(token: TokenMeta, base_assets: Mapping[str, str]) -> RiskFlag:
    """计算 token 的风险标记。

    @param token token 元数据；`risk_flag` 已有值（例如人工标记）时直接返回它
    @param base_assets 基础资产清单：小写地址 → symbol（来自链画像）；原生币用键 NATIVE，
        这样仿冒原生币符号（例如假 BNB token）也能识别
    @returns 风险标记
    """
    if token.risk_flag is not None:
        return token.risk_flag
    address = token.address.lower()
    if address in base_assets:
        return RiskFlag.NORMAL
    texts = [t for t in (token.symbol, token.name) if t]
    if any(_SPAM_PATTERN.search(t) for t in texts):
        return RiskFlag.SPAM
    if token.symbol:
        normalized = normalize_symbol(token.symbol)
        if normalized and normalized in {normalize_symbol(s) for s in base_assets.values()}:
            return RiskFlag.IMPERSONATOR
    return RiskFlag.NORMAL
