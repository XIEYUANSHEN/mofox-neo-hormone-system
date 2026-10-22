"""赛博内分泌系统（hormone_system）插件入口。

订阅 KFC 的 on_prompt_build 事件，当目标为 ``kfc_user_prompt`` 时：

1. 按 stream_id 载入该会话的情绪状态；
2. 结算空闲时长的半衰期衰减，并对用户消息打分、
   更新情绪记忆窗口、疲劳度与六激素；
3. 生成自然语言心境块（无数字、无激素名）追加到 ``values.extra``；
4. 原子写回状态文件。

任何异常都静默降级（只告警），绝不影响 KFC 正常回复。
"""

from __future__ import annotations

import logging
from typing import Any

from src.app.plugin_system.base import (
    BaseEventHandler,
    BasePlugin,
    register_plugin,
)
from src.app.plugin_system.api.event_api import EventDecision

from .config import HormoneSystemConfig
from .hormone import HormoneSystem
from .prompt import build_mood_prompt
from .state_store import HormoneStateStore

logger = logging.getLogger("hormone_system")

# KFC 的 user prompt 模板名
_KFC_USER_PROMPT = "kfc_user_prompt"


class HormonePromptHandler(BaseEventHandler):
    """赛博内分泌事件处理器：结算激素衰减并注入心境到 KFC prompt。"""

    handler_name: str = "hormone_prompt_handler"
    handler_description: str = "为 KFC 角色提供情绪惯性：结算激素衰减并注入身心状态心境"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_prompt_build"]

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self._store: HormoneStateStore | None = None

    def _get_config(self) -> HormoneSystemConfig:
        config = getattr(self.plugin, "config", None)
        if isinstance(config, HormoneSystemConfig):
            return config
        return HormoneSystemConfig()

    def _get_store(self, config: HormoneSystemConfig) -> HormoneStateStore:
        """懒初始化状态存储（目录取配置，变更后重新初始化）。"""
        if self._store is None or self._store.state_dir != config.core.state_dir:
            self._store = HormoneStateStore(config.core.state_dir)
        return self._store

    @staticmethod
    def _extract_values_field(values: Any, key: str, default: Any = None) -> Any:
        """从事件 values 读字段，兼容 dict / object 两种访问方式。"""
        if isinstance(values, dict):
            return values.get(key, default)
        return getattr(values, key, default)

    @staticmethod
    def _extract_message_text(values: Any) -> str:
        """从事件 values 提取用户消息文本。

        content 可能是 str 或 list[{"type":"text","text":...}]；
        同时剥离 KFC 的 last-mile 指令尾段（--- 分隔符后的部分）。
        """
        content = HormonePromptHandler._extract_values_field(values, "content")
        if content is None:
            return ""
        if isinstance(content, list):
            # list[dict] 结构：提取 text 类型的内容段
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            content = "\n".join(parts)
        elif not isinstance(content, str):
            content = str(content)

        # 剥离 KFC last-mile 指令（"\n\n---\n" 后的行为指令部分）
        separator_idx = content.rfind("\n\n---\n")
        if separator_idx >= 0:
            content = content[:separator_idx]

        return content

    @staticmethod
    def _append_extra(values: Any, injection: str) -> None:
        """追加到 values.extra（不覆盖其他插件已注入的内容）。"""
        if isinstance(values, dict):
            existing = values.get("extra", "") or ""
            values["extra"] = (
                (existing + "\n\n" + injection) if existing else injection
            )
        else:
            existing = getattr(values, "extra", "") or ""
            values.extra = (
                (existing + "\n\n" + injection) if existing else injection
            )

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 on_prompt_build 事件：结算激素 + 注入心境。

        流程：
        1. 过滤非 kfc_user_prompt 事件
        2. 载入 stream 状态并结算空闲衰减
        3. 有用户消息时：打分 → 情绪记忆 → 疲劳 → 激素响应
        4. 生成心境块追加到 values.extra，立即原子保存状态
        """
        # 仅处理 KFC 的 prompt
        if params.get("name") != _KFC_USER_PROMPT:
            return EventDecision.SUCCESS, params

        try:
            config = self._get_config()
            if not config.core.enabled:
                return EventDecision.SUCCESS, params

            values = params.get("values")
            if values is None:
                return EventDecision.SUCCESS, params
            stream_id = str(
                self._extract_values_field(values, "stream_id", "") or ""
            )
            message_text = self._extract_message_text(values)

            store = self._get_store(config)
            system = HormoneSystem(config)
            data = store.load(stream_id)
            if data:
                system.apply_state(data)

            # 无新消息（续轮/超时）只结算衰减与注入，不打分
            if message_text.strip():
                system.update_hormone_by_text(message_text)

            self._append_extra(values, build_mood_prompt(system, config))
            store.save(stream_id, system.to_state(config.core.state_version))

            # 每轮最多 1 行 INFO：六个激素值 + 疲劳度
            h = system.hormones
            logger.info(
                "hormone DA=%.1f CORT=%.1f OXY=%.1f SER=%.1f END=%.1f EMP=%.1f "
                "fatigue=%.1f stream=%s",
                h["dopamine"], h["cortisol"], h["oxytocin"],
                h["serotonin"], h["endorphin"], h["empathy"],
                system.fatigue, stream_id or "default",
            )

        except Exception as exc:  # noqa: BLE001 - 异常不能影响 KFC 正常回复
            logger.warning("激素系统处理异常: %s", exc)

        return EventDecision.SUCCESS, params


@register_plugin
class HormoneSystemPlugin(BasePlugin):
    """赛博内分泌系统插件。"""

    plugin_name: str = "hormone_system"
    plugin_description: str = "为 KFC 角色提供情绪惯性与疲劳度"
    plugin_version: str = "0.1.0"

    configs: list[type] = [HormoneSystemConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        config = getattr(self, "config", None)
        if isinstance(config, HormoneSystemConfig) and not config.core.enabled:
            logger.info("hormone_system 已在配置中禁用")
            return []
        return [HormonePromptHandler]
