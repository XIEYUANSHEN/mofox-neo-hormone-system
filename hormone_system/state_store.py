"""激素系统按 stream 的状态持久化。

路径为 ``<state_dir>/<stream_id>.json``：
- 原子写：先写 ``.tmp`` 再 ``os.replace``；
- 状态损坏或结构异常时返回 None，由调用方重置为基线，绝不抛异常
  （不能因为状态文件坏了让她不回复）。
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger("hormone_system")

_SAFE_ID = re.compile(r"[^A-Za-z0-9_\-]")
_MAX_ID_LEN = 64


def _sanitize_stream_id(stream_id: str) -> str:
    """把 stream_id 规整为文件名安全标识；空值回退 default。"""
    if not stream_id or not stream_id.strip():
        return "default"
    safe = _SAFE_ID.sub("_", stream_id.strip())[:_MAX_ID_LEN]
    return safe or "default"


class HormoneStateStore:
    """单个 stream 状态文件的读写（原子写、损坏宽容）。"""

    def __init__(self, state_dir: str = "data/plugins/hormone_system") -> None:
        self._state_dir = state_dir

    @property
    def state_dir(self) -> str:
        """状态存储目录（供调用方比对配置变更）。"""
        return self._state_dir

    def state_path(self, stream_id: str) -> str:
        """该 stream 对应的状态文件路径。"""
        return os.path.join(
            self._state_dir, f"{_sanitize_stream_id(stream_id)}.json"
        )

    def load(self, stream_id: str) -> dict | None:
        """读取状态字典；文件不存在或损坏时返回 None（调用方重置基线）。"""
        path = self.state_path(stream_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("状态文件根节点不是对象")
            return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("激素状态文件损坏，将重置为基线 (%s): %s", path, exc)
            return None

    def save(self, stream_id: str, state: dict) -> None:
        """原子写状态：写 .tmp 后 os.replace。失败只告警不抛异常。"""
        path = self.state_path(stream_id)
        tmp = path + ".tmp"
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("激素状态保存失败 (%s): %s", path, exc)
