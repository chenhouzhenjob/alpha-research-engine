"""OHLCV 特征协议与 Parquet 特征存储。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

_INDEX = ["ts_event_ms", "instrument_id"]
FeatureInputs = Mapping[str, pd.DataFrame]


class Feature(Protocol):
    """将一个或多个 canonical 数据集确定性转换为一个具名特征列。"""

    name: str
    required_datasets: frozenset[str]

    def compute(self, inputs: FeatureInputs) -> pd.Series: ...


@dataclass(frozen=True)
class FeatureSet:
    name: str
    features: tuple[Feature, ...]
    anchor_dataset: str = "ohlcv"


def compute_features(inputs: FeatureInputs, feature_set: FeatureSet) -> pd.DataFrame:
    """以锚点数据集的时间/标的坐标计算多数据集特征。"""
    if feature_set.anchor_dataset not in inputs:
        raise ValueError(f"缺少锚点数据集: {feature_set.anchor_dataset}")
    anchor = inputs[feature_set.anchor_dataset]
    missing = set(_INDEX) - set(anchor.columns)
    if missing:
        raise ValueError(f"锚点数据集缺少字段: {sorted(missing)}")
    ordered = anchor.sort_values(["instrument_id", "ts_event_ms"]).copy()
    normalized: dict[str, pd.DataFrame] = dict(inputs)
    normalized[feature_set.anchor_dataset] = ordered
    result = ordered[_INDEX].copy()
    for feature in feature_set.features:
        missing_datasets = feature.required_datasets - set(normalized)
        if missing_datasets:
            raise ValueError(f"feature {feature.name} 缺少数据集: {sorted(missing_datasets)}")
        value = feature.compute(normalized)
        if not value.index.equals(ordered.index):
            raise ValueError(f"feature {feature.name} 返回了不匹配的索引")
        result[feature.name] = value
    return result.sort_values(_INDEX).reset_index(drop=True)


def _feature_path(data_dir: str | Path, feature_set: str) -> Path:
    return Path(data_dir) / "features" / feature_set / "features.parquet"


def write_features(data_dir: str | Path, feature_set: str, rows: pd.DataFrame) -> Path:
    """在一个特征集的数据集边界内原子替换结果。"""
    if not set(_INDEX).issubset(rows.columns):
        raise ValueError("feature rows 必须包含 ts_event_ms 与 instrument_id")
    path = _feature_path(data_dir, feature_set)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(path, index=False)
    return path


def load_features(data_dir: str | Path, feature_set: str) -> pd.DataFrame:
    path = _feature_path(data_dir, feature_set)
    if not path.exists():
        return pd.DataFrame(columns=_INDEX)
    return pd.read_parquet(path).sort_values(_INDEX).reset_index(drop=True)
