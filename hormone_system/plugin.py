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

import json
import logging
import asyncio
import os
import re
import time
from typing import Any

from src.app.plugin_system.base import (
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    cmd_route,
    register_plugin,
)
from src.app.plugin_system.api.event_api import EventDecision

from .config import HormoneSystemConfig
from .hormone import HormoneSystem
from .prompt import build_mood_prompt
from .state_store import HormoneStateStore

try:
    # 使用框架日志接口：enable_event_broadcast 会把日志广播到后台日志面板
    from src.app.plugin_system.api.log_api import get_logger

    logger = get_logger("hormone_system", display="赛博内分泌")
except Exception:  # pragma: no cover - 仅在脱离框架跑离线单测时生效
    import logging

    logger = logging.getLogger("hormone_system")

# 按 stream 串行化：on_prompt_build 与夜间整理调度任务会读写同一份 per-stream json
_STREAM_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(stream_id: str) -> asyncio.Lock:
    """取（或懒建）某 stream 的 asyncio 锁。"""
    lock = _STREAM_LOCKS.get(stream_id)
    if lock is None:
        lock = asyncio.Lock()
        _STREAM_LOCKS[stream_id] = lock
    return lock

# KFC 的 user prompt 模板名
HORMONE_CN = {
    "dopamine": "多巴胺",
    "cortisol": "皮质醇",
    "oxytocin": "催产素",
    "serotonin": "血清素",
    "endorphin": "内啡肽",
    "empathy": "共情",
}

_KFC_USER_PROMPT = "kfc_user_prompt"

_SCORE_PROMPT = (
    "你在给一句对话打情绪刺激分。判断这句话会让'她'产生哪几种情绪，各自多强。\n"
    "\n"
    "可选通道（只写这句话真正触发的，没触发就不要出现）：\n"
    "- hurt       被伤害：指责、贬低、否定、冷漠、背叛、威胁\n"
    "- pressure   被施压：追问、逼迫、翻旧账、道德绑架\n"
    "- warmth     被暖到：亲昵、告白、撒娇、夸奖、想念\n"
    "- care       被照顾：道歉、关心、哄、示弱\n"
    "- vulnerable 对方脆弱：他倾诉、求助、说累/难过/撑不住（这会唤起她的共情，不是伤害）\n"
    "\n"
    "强度范围 0.0-1.0。一句话可以同时触发多个通道（例如'我气你不说，但我还是爱你'"
    "→ hurt 0.5 + warmth 0.7）。各通道独立，不要互相抵消。\n"
    "\n"
    "只输出 JSON，不要解释、不要 markdown。例：\n"
    '{"hurt": 0.6, "warmth": 0.7}\n'
    "完全中性输出：{}"
)

_SCORE_RE = re.compile(r"\{.*\}", re.S)


def _clip_text(text: str, limit: int) -> str:
    """超长消息保留头尾各一半——避免狠话放在结尾时被截掉。"""
    if limit <= 0 or len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + chr(10) + "…" + chr(10) + text[-half:]


def _parse_emotions(raw: str) -> dict[str, float] | None:
    """从模型输出里提取 JSON 对象；失败返回 None（调用方回退词典）。"""
    text = (raw or "").strip()
    if not text:
        return None
    # 先剥 markdown 围栏
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    # 直接试
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # 从每个 { 位置 raw_decode（容忍前后废话）
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = text.find("{", idx)
        if idx == -1:
            return None
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx += 1



_DREAM_PROMPT = (
    "你要替'她'写一段今晚睡觉时可能做的梦，第一人称、口吻贴合角色，控制在 60 字内，"
    "不出现任何数字和激素名称。\n"
    "基调（tone）会告诉你梦偏甜(sweet)/偏中性(neutral)/甜虐交织(ambivalent)；"
    "下面附上她白天情绪波动最大的几件事作为素材，梦可以从里面延伸、变形，但不必逐条复述。\n"
    "同时自评两样：recall=梦醒来还记得多清楚（clear/fuzzy/none）；worth=值不值得记进'小本本'（true/false）。\n"
    '只输出 JSON，不要解释、不要 markdown。例：{"dream":"…","recall":"fuzzy","worth":true}'
)


async def _generate_dream(
    config: HormoneSystemConfig, items: list[dict], tone: str
) -> dict | None:
    """用 consolidate_model_task（留空则跟随主回复 actor）生成一段梦；
    任务与 actor 都不可用 / 超时 / 不可解析 → 返回 None（跳过今晚，绝不影响回复）。
    """
    task_name = (config.sleep.consolidate_model_task or "").strip() or "actor"
    try:
        from src.app.plugin_system.api.llm_api import (
            create_llm_request,
            get_model_set_by_task,
        )
        from src.app.plugin_system.types import LLMPayload, ROLE, Text

        try:
            model_set = get_model_set_by_task(task_name)
        except Exception:
            if task_name == "actor":
                raise
            logger.warning(f"整理模型任务 '{task_name}' 不可用，回退 actor")
            model_set = get_model_set_by_task("actor")
        material = "\n".join(
            f"- {it.get('text', '')}" for it in items if it.get("text")
        )
        user_text = f"基调：{tone}\n白天她在意的事：\n{material}"
        request = create_llm_request(model_set, "hormone_dream")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_DREAM_PROMPT)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_text)))
        result = await asyncio.wait_for(
            request.send(stream=False),
            timeout=config.sleep.consolidate_timeout_seconds,
        )
        raw = (await result or "").strip()
        parsed = _parse_emotions(raw)  # 复用宽松 JSON 提取
        if not isinstance(parsed, dict) or not parsed.get("dream"):
            logger.debug(f"梦生成无法解析: {raw[:60]!r}")
            return None
        recall = str(parsed.get("recall", "fuzzy"))
        if recall not in ("clear", "fuzzy", "none"):
            recall = "fuzzy"
        return {
            "dream": str(parsed.get("dream", ""))[:160],
            "recall": recall,
            "worth": bool(parsed.get("worth", False)),
        }
    except Exception as exc:  # noqa: BLE001 - 生成失败不能影响任何链路
        logger.debug(f"梦生成失败，跳过今晚: {exc}")
        return None


def _in_pre_wake_window(system: HormoneSystem, now: float, config: HormoneSystemConfig) -> bool:
    """now 是否落在临醒窗口 [wake_time - dream_pre_wake_hours, wake_time)。"""
    cfg = config.sleep
    if not cfg.consolidate or not cfg.wake_time:
        return False
    hour = system._hours_since_midnight(now)
    start = cfg.wake_time - cfg.dream_pre_wake_hours
    end = cfg.wake_time
    if start < 0.0:
        return hour >= start + 24.0 or hour < end
    return start <= hour < end


class HormonePromptHandler(BaseEventHandler):
    """赛博内分泌事件处理器：结算激素衰减并注入心境到 KFC prompt。"""

    name: str = "hormone_prompt_handler"
    description: str = "为 KFC 角色提供情绪惯性：结算激素衰减并注入身心状态心境"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_prompt_build"]

    def __init__(self, plugin: Any) -> None:
        """初始化事件处理器。"""
        super().__init__(plugin)
        self._store: HormoneStateStore | None = None

    def _get_config(self) -> HormoneSystemConfig:
        """取插件配置，缺失时返回默认配置。"""
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

    async def _score_with_llm(self, text: str, config: HormoneSystemConfig) -> dict[str, Any] | None:
        """用 LLM 给用户消息打情绪分；返回情绪通道 dict，失败/超时返回 None（调用方回退词典）。"""
        if not config.score.enabled:
            return None
        try:
            from src.app.plugin_system.api.llm_api import (
                create_llm_request,
                get_model_set_by_task,
            )
            from src.app.plugin_system.types import LLMPayload, ROLE, Text

            # 打分模型：配置为空则跟随对话模型（actor），配了就用自己的
            task_name = (config.score.model_task or "").strip() or "actor"
            try:
                model_set = get_model_set_by_task(task_name)
            except Exception:
                if task_name == "actor":
                    raise
                logger.warning(f"打分模型任务 '{task_name}' 不可用，回退 actor")
                model_set = get_model_set_by_task("actor")
            request = create_llm_request(model_set, "hormone_score")
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SCORE_PROMPT)))
            request.add_payload(
                LLMPayload(ROLE.USER, Text(_clip_text(text, config.score.max_input_chars)))
            )
            result = await asyncio.wait_for(
                request.send(stream=False),
                timeout=config.score.timeout_seconds,
            )
            raw = (await result or "").strip()
            emotions = _parse_emotions(raw)
            if emotions is None:
                logger.debug(f"LLM 打分无法解析: {raw[:60]!r}")
            return emotions
        except Exception as exc:  # noqa: BLE001 - 打分失败不能影响回复
            logger.debug(f"LLM 打分失败，回退词典: {exc}")
            return None

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
            # 与夜间整理调度任务串行化：锁 key 用落盘路径，两侧对同一文件一致
            async with _lock_for(store.state_path(stream_id)):
                data = store.load(stream_id)
                if data:
                    system.apply_state(data)

                # 记录本轮结算前的状态，用于日志展示变化量
                before_h = dict(system.hormones)
                before_fatigue = system.fatigue

                # 无新消息（续轮/超时）只结算衰减与注入，不打分
                if message_text.strip():
                    emotions = await self._score_with_llm(message_text, config)
                    if emotions is None:
                        system.update_hormone_by_text(message_text)
                    else:
                        system.update_hormone_by_emotions(message_text, emotions)

                self._append_extra(values, build_mood_prompt(system, config))
                store.save(stream_id, system.to_state(config.core.state_version))

            # 每轮 1 行 INFO：当前激素状态 + 本轮变化量
            h = system.hormones
            parts = []
            for k in HORMONE_CN:
                delta = h[k] - before_h.get(k, h[k])
                if abs(delta) >= 0.5:
                    parts.append("%s=%.0f(%+.0f)" % (HORMONE_CN[k], h[k], delta))
                else:
                    parts.append("%s=%.0f" % (HORMONE_CN[k], h[k]))
            logger.info(
                f"状态={system.describe_state()} | 激素 {'  '.join(parts)} | "
                f"疲劳={system.fatigue:.0f}({system.fatigue - before_fatigue:+.0f}) | "
                f"会话={(stream_id or 'default')[:8]}"
            )

        except Exception as exc:  # noqa: BLE001 - 异常不能影响 KFC 正常回复
            logger.warning(f"激素系统处理异常: {exc}")

        return EventDecision.SUCCESS, params


class DreamReviewCommand(BaseCommand):
    """/查看昨晚梦境：只读展示她最近一晚记住的梦与小本本，供用户审计（不主动外泄）。"""

    name: str = "查看昨晚梦境"
    description: str = "查看她最近一晚记住的梦/惦记（只读审计）"

    @cmd_route()
    async def handle_review(self) -> tuple[bool, str]:
        """读取当前 stream 持久化状态，返回最近一晚梦境/小本本摘要。"""
        try:
            cfg = getattr(self, "plugin", None)
            cfg = getattr(cfg, "config", None) if cfg else None
            if not isinstance(cfg, HormoneSystemConfig):
                cfg = HormoneSystemConfig()
            store = HormoneStateStore(cfg.core.state_dir)
            sid = str(getattr(self, "stream_id", "") or "")
            data = store.load(sid)
            if not data:
                return True, "（翻了翻小本本）……昨晚好像没记下什么。"
            system = HormoneSystem(cfg)
            system.apply_state(data)
            return True, self._format(system)
        except Exception as exc:  # noqa: BLE001 - 审计命令不能抛错
            logger.warning(f"查看昨晚梦境失败: {exc}")
            return True, "（有点恍惚，想不太起来了）"

    @staticmethod
    def _format(system: HormoneSystem) -> str:
        """把 gen_dream / 噩梦阴影 / 小本本 拼成一段可读文本。"""
        parts: list[str] = []
        gd = system.gen_dream
        if isinstance(gd, dict) and gd.get("text"):
            tone_label = {
                "sweet": "甜甜的", "ambivalent": "又暖又酸", "neutral": "有点模糊",
            }.get(str(gd.get("tone", "neutral")), "有点模糊")
            recall = gd.get("recall", "fuzzy")
            if recall == "fuzzy":
                parts.append(f"（其实记得很模糊了）好像做了个{tone_label}的梦：{gd.get('text', '')}")
            elif recall != "none":
                parts.append(f"昨晚记得比较清楚——{gd.get('text', '')}")
        if system.sleep_quality == "bad" and system.sleep_thoughts:
            parts.append(f"（心底还压着一点影子：{system.sleep_thoughts}）")
        if system.sleep_journal:
            hist = "；".join(
                j.get("text", "") for j in system.sleep_journal if isinstance(j, dict) and j.get("text")
            )
            if hist:
                parts.append(f"最近记着的：{hist}")
        if not parts:
            return "（翻了翻小本本）……昨晚好像没记下什么。"
        return "\n".join(parts)


@register_plugin
class HormoneSystemPlugin(BasePlugin):
    """赛博内分泌系统插件。"""

    plugin_name: str = "hormone_system"

    configs: list[type] = [HormoneSystemConfig]
    dependent_components: list[str] = []

    def __init__(self, config: HormoneSystemConfig | None = None) -> None:
        """初始化插件，记录调度任务 id 与后台注册任务 id。"""
        super().__init__(config)
        self._schedule_ids: list[str] = []
        self._register_task_id: str | None = None

    def get_components(self) -> list[type]:
        """返回插件提供的组件类列表。"""
        config = getattr(self, "config", None)
        if isinstance(config, HormoneSystemConfig) and not config.core.enabled:
            logger.info("hormone_system 已在配置中禁用")
            return []
        return [HormonePromptHandler, DreamReviewCommand]

    async def on_plugin_loaded(self) -> None:
        """加载后：若开启睡眠整理，等待调度器就绪再注册周期任务。"""
        cfg = getattr(self, "config", None)
        if not isinstance(cfg, HormoneSystemConfig):
            return
        if not cfg.sleep.consolidate:
            return
        from src.kernel.concurrency import get_task_manager

        tm = get_task_manager()
        task = tm.create_task(
            self._register_schedule_when_ready(),
            name="hormone_sleep_register",
            daemon=True,
        )
        self._register_task_id = task.task_id

    async def _register_schedule_when_ready(self) -> None:
        """等待 scheduler 启动后注册临醒前梦生成周期任务（scheduler 在 run() 才 start）。"""
        from src.kernel.scheduler import TriggerType, get_unified_scheduler

        cfg = self.config
        interval = max(60, int(cfg.sleep.dream_check_minutes) * 60)
        while True:
            try:
                scheduler = get_unified_scheduler()
                sid = await scheduler.create_schedule(
                    callback=self._consolidate_job,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name="hormone_sleep_dream",
                    force_overwrite=True,
                )
                self._schedule_ids = [sid]
                logger.info(f"hormone_system 梦生成任务已注册: {sid}（每 {interval // 60} 分钟检查临醒窗口）")
                return
            except RuntimeError:
                await asyncio.sleep(0.5)
            except Exception as exc:  # noqa: BLE001 - 注册失败不影响插件其余功能
                logger.warning(f"注册梦生成任务失败: {exc}")
                return

    async def _consolidate_job(self) -> None:
        """周期任务：临醒窗口内为各 stream 生成当晚的梦；模型调用在锁外，避免阻塞回复。"""
        cfg = getattr(self, "config", None)
        if not isinstance(cfg, HormoneSystemConfig) or not cfg.sleep.consolidate:
            return
        store = HormoneStateStore(cfg.core.state_dir)
        now = time.time()
        if not os.path.isdir(store.state_dir):
            return
        for fn in os.listdir(store.state_dir):
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            try:
                key = store.state_path(sid)
                # 临界区1（短，不含网络）：读态 + 判窗口 + 去重 + 取原料
                async with _lock_for(key):
                    data = store.load(sid)
                    if not data:
                        continue
                    probe = HormoneSystem(cfg)
                    probe.apply_state(data)
                    if not _in_pre_wake_window(probe, now, cfg):
                        continue
                    window_start = probe._day_anchor(
                        now, max(cfg.sleep.wake_time - cfg.sleep.dream_pre_wake_hours, 0.0)
                    )
                    gd = probe.gen_dream
                    if isinstance(gd, dict) and float(gd.get("ts", 0.0)) >= window_start:
                        continue  # 本窗口已生成过
                    items, tone = probe.get_generation_input()
                    if not items or tone in (None, "nightmare"):
                        continue  # 噩梦交回既有阴影路径；无原料不生成

                # 锁外调模型（可能耗时）→ 不占用 stream 锁、不阻塞回复
                gen = await _generate_dream(cfg, items, tone)
                if not gen:
                    continue

                # 临界区2（短）：重载最新态 → 二次去重 → 应用 → 存
                async with _lock_for(key):
                    fresh = store.load(sid) or {}
                    system = HormoneSystem(cfg)
                    if fresh:
                        system.apply_state(fresh)
                    gd2 = system.gen_dream
                    if isinstance(gd2, dict) and float(gd2.get("ts", 0.0)) >= window_start:
                        continue  # 等模型这段时间里可能已生成过
                    system.apply_generated_dream(gen["dream"], tone, gen["recall"], gen["worth"])
                    store.save(sid, system.to_state(cfg.core.state_version))
                    logger.info(
                        f"[Dream] stream={sid[:8]} tone={tone} recall={gen['recall']} worth={gen['worth']}"
                    )
            except Exception as exc:  # noqa: BLE001 - 单流失败不影响其它流
                logger.warning(f"梦生成处理 stream {sid[:8]} 失败: {exc}")
                continue

    async def on_plugin_unloaded(self) -> None:
        """卸载前：移除调度任务、取消后台注册协程。"""
        from src.kernel.scheduler import get_unified_scheduler

        scheduler = get_unified_scheduler()
        for sid in list(self._schedule_ids):
            try:
                await scheduler.remove_schedule(sid)
            except Exception:
                pass
        self._schedule_ids.clear()
        if self._register_task_id:
            try:
                from src.kernel.concurrency import get_task_manager

                get_task_manager().cancel_task(self._register_task_id)
            except Exception:
                pass
            self._register_task_id = None
