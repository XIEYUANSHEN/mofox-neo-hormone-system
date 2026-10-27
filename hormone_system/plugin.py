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
from .hormone import INTERNAL_MARKERS, HormoneSystem
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


# KFC 转发信封：`[新消息]\n》2026-10-24 13:52:46》[QQ:2750694203] 中生 [消息id:532868125]：`
# 只留「昵称：正文」——QQ 号/时间/消息 id 对情绪打分和入梦原料都是噪声，
# 但昵称必须留：梦是第一人称写的，模型手里没有别的线索能知道"这些事是跟谁发生的"。
_RELAY_ENVELOPE = re.compile(
    r"(?:\[新消息\]\s*)?(?:》[^》]{0,60}》\s*)*(?:\[QQ:\s*\d+\]\s*)?"
    r"(?P<name>[^\[\]\n：:]{0,40}?)\s*\[消息id[:：]\s*[^\]\n]{1,64}\]\s*[：:]?\s*",
    re.S,
)
# 引用回复的嵌套壳：`[回复<昵称(id)：被引的原话]，说：真正说出口的话`
# 末尾那个 `[回复<…` 不留 `]` 的，是被上游截断的半截壳，一并清掉。
_REPLY_PREFIX = re.compile(
    r"^\s*(?:\[回复<[^\]\n]{0,160}\](?:\s*[，,]\s*说\s*[：:])?\s*|\[回复<[^\n]*$)\s*"
)
_SPEAKER_MAX = 12


def _normalize_relay_text(text: str) -> str:
    """把 KFC 转发信封压成「昵称：正文」；普通消息原样返回。

    - 只认 `[消息id:…]` 这个信封标记，不会误伤正常聊天里的冒号；
    - 昵称保留、过长截到 12 字；引用壳剥掉，只留真正说出口的那句；
    - 剥完没有正文（只有壳/只有引用）时返回空串，由调用方跳过这条原料，
      绝不把纯壳当正文喂给模型。
    """
    src = text or ""
    matches = list(_RELAY_ENVELOPE.finditer(src))
    if not matches:
        return src
    last = matches[-1]
    body = _REPLY_PREFIX.sub("", src[last.end():])
    body = " ".join(body.split())
    name = " ".join((last.group("name") or "").split())
    if len(name) > _SPEAKER_MAX:
        name = name[:_SPEAKER_MAX]
    if not body:
        return ""
    return f"{name}：{body}" if name else body


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
    "你要替'她'写一段今晚睡觉时做的梦：第一人称、口吻贴合角色，40–70 字，"
    "不出现任何数字和激素名称。\n"
    "素材是她这几天说过、听过的一些对话片段（'他'指对方、'我'指她自己），来自不同的时候，顺序是打乱的。\n"
    "梦是碎的、不讲逻辑的：可以换场景、把人或事缝在一起、跳过因果，也可以只留一个画面或一句话。\n"
    "**不要照抄原句，也不要把发生过的事按原样讲一遍**——最多留一个画面或一句语气，其余都要变形；"
    "不要总结，也不必把每段都写进去。\n"
    "基调（tone）告诉你梦偏甜(sweet)/偏中性(neutral)/甜虐交织(ambivalent)。"
    "如果素材里带着冲突、委屈或没说完的话，梦里要留着它，不要只挑好的一面。\n"
    '只输出 JSON，不要解释、不要 markdown。例：{"dream":"…"}'
)


def _coerce_dream(parsed: dict | None, raw: str | None = None) -> dict | None:
    """把模型输出规整为 {dream, recall, worth}；非 dict 或 dream 空 → None。

    recall 归一到 clear/fuzzy/none；worth 兼容字符串("false"等)与布尔。
    模型偶尔把 JSON 截断（"dream":"… 后面没闭合），此时从原文里把 dream 抠出来，
    不让一夜的梦白丢。
    """
    dream = ""
    if isinstance(parsed, dict):
        dream = str(parsed.get("dream", "") or "")
    if not dream and raw:
        m = re.search(r'"dream"\s*:\s*"(.+)', raw, re.S)
        if m:
            dream = m.group(1).split('"')[0].strip()
    if not dream:
        return None
    recall = str((parsed or {}).get("recall", "fuzzy"))
    if recall not in ("clear", "fuzzy", "none"):
        recall = "fuzzy"
    raw_worth = (parsed or {}).get("worth", False)
    if isinstance(raw_worth, str):
        worth = raw_worth.strip().lower() in ("true", "1", "yes", "是")
    else:
        worth = bool(raw_worth)
    return {"dream": dream[:160], "recall": recall, "worth": worth}


# 整理类调用（做梦/定作息/补觉）的模型任务候选链：首选没成就往后退。
# 为什么要有链：这些调用默认跟随主回复模型（actor），主模型欠费/挂掉时，
# 熬夜里的梦、作息、补觉会一起静默消失——它们并不是非 actor 不可。
_CONSOLIDATE_TASK_CHAIN = ("actor", "sub_actor", "utils_small", "utils")


def _consolidate_tasks(config: HormoneSystemConfig) -> tuple[str, ...]:
    """整理调用依次尝试的模型任务名：配置的任务优先，再走内置兜底链（去重）。"""
    named = (config.sleep.consolidate_model_task or "").strip()
    chain: list[str] = []
    for task in ([named] if named else []) + list(_CONSOLIDATE_TASK_CHAIN):
        if task and task not in chain:
            chain.append(task)
    return tuple(chain)


def _reason(text: Any, limit: int = 90) -> str:
    """把异常/回包压成一行能给人看的原话（空异常也要有字）。"""
    s = " ".join(str(text).split())
    if not s:
        return "无详细信息"
    return s if len(s) <= limit else s[:limit] + "…"


async def _call_consolidate_llm(
    config: HormoneSystemConfig,
    system_text: str,
    user_text: str,
    request_name: str,
    report: dict | None = None,
) -> str | None:
    """按候选任务链向模型要一段文本；全都没成就返回 None，并把原因写进 report["error"]。

    - 单个任务最多等 consolidate_timeout_seconds，等不到就换下一个备用任务；
    - 整条链共享 consolidate_total_seconds 总预算，绝不让后台整理拖成长任务；
    - 只有前面的任务失败（任务不存在/接口报错/超时/空回复）才会试下一个；
    - 任何情况下都不抛异常出去，后台整理不能影响正常回复。
    """
    try:
        from src.app.plugin_system.api.llm_api import (
            create_llm_request,
            get_model_set_by_task,
        )
        from src.app.plugin_system.types import LLMPayload, ROLE, Text
    except Exception as exc:  # noqa: BLE001 - 脱离框架跑离线测试时属正常
        if report is not None:
            report["error"] = f"框架 LLM 接口不可用（{type(exc).__name__}）"
        return None

    chain = _consolidate_tasks(config)
    per = max(1.0, float(config.sleep.consolidate_timeout_seconds))
    total = max(per, float(getattr(config.sleep, "consolidate_total_seconds", per)))
    end = time.monotonic() + total
    reasons: list[str] = []
    for idx, task in enumerate(chain):
        left = end - time.monotonic()
        if left < 1.0:
            reasons.append(f"{task}: 总时间预算用完")
            break
        try:
            model_set = get_model_set_by_task(task)
            request = create_llm_request(model_set, request_name)
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_text)))
            request.add_payload(LLMPayload(ROLE.USER, Text(user_text)))
            result = await asyncio.wait_for(
                request.send(stream=False), timeout=min(per, left)
            )
            raw = ((await result) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 这个任务不成，换下一个
            reasons.append(f"{task}: {type(exc).__name__} {_reason(exc)}".strip())
            continue
        if not raw:
            reasons.append(f"{task}: 空回复")
            continue
        if idx > 0:
            logger.info(
                f"[Model] {request_name} 换用备用模型任务 {task}"
                f"（前面 {'/'.join(chain[:idx])} 没成）"
            )
        return raw
    err = "；".join(reasons) or "无可用模型任务"
    if report is not None:
        report["error"] = err
    logger.debug(f"[Model] {request_name} 各任务都没成: {_reason(err, 200)}")
    return None


async def _generate_dream(
    config: HormoneSystemConfig,
    items: list[dict],
    tone: str,
    report: dict | None = None,
) -> dict | None:
    """用模型生成一段梦（只要内容；清不清楚、值不值得记留给醒来那一刀判定）。

    任务链全挂 / 超时 / 不可解析 → 返回 None（今晚跳过，绝不影响回复），
    失败原因塞进 report["error"]，由调用方决定怎么说、说几次。
    """
    material = "\n".join(
        f"- {it.get('text', '')}" for it in items if it.get("text")
    )
    user_text = f"基调：{tone}\n素材（来自不同时间、顺序已打乱）：\n{material}"
    raw = await _call_consolidate_llm(
        config, _DREAM_PROMPT, user_text, "hormone_dream", report=report
    )
    if raw is None:
        return None
    out = _coerce_dream(_parse_emotions(raw), raw)
    if out is None:
        logger.debug(f"梦生成无法解析: {raw[:60]!r}")
        if report is not None:
            report["error"] = f"模型回的不是梦：{_reason(raw, 60)}"
    return out


_PLAN_PROMPT = (
    "你在替'她'决定今晚几点睡、睡多久。参考现在的时刻，以及她今天有多累、心情如何。\n"
    "推荐睡约 6 小时：睡少会影响明天状态（更累、易烦）；睡多可以补觉，但睡过头会睡懵。\n"
    "如果此刻还在和对方聊，可以定晚一点、甚至先不定。\n"
    "就寝落在 20:00–次日06:00 之间、时长 1–12 小时（这是防呆范围，不是硬性要求）。\n"
    '只输出 JSON，不要解释、不要 markdown。例：{"bedtime":"23:40","duration_h":6.5,"note":"今天有点累"}'
)


def _hour_from(value: Any) -> float | None:
    """把 "23:40" 或数字解析成小时（float）；失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$", value)
        if m:
            return int(m.group(1)) + int(m.group(2)) / 60.0
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _fallback_plan(system: HormoneSystem) -> dict:
    """模型不可用时的确定性兜底：越累越早睡、睡越久。"""
    now = time.time()
    hour = system._hours_since_midnight(now)
    fatigue = system.fatigue
    base = 23.0 - max(0.0, fatigue - 50.0) / 50.0 * 1.5
    dur = 6.0 + max(0.0, fatigue - 30.0) / 70.0 * 1.5
    # 若此刻已经很晚，就寝顺延到当前之后
    if hour > base:
        base = min(hour + 0.5, 23.9)
    return {"bedtime": base, "duration_h": dur, "note": "fallback", "source": "fallback"}


async def _generate_sleep_plan(
    config: HormoneSystemConfig, system: HormoneSystem, report: dict | None = None
) -> dict:
    """用模型定今晚作息；失败/超时/不可解析 → 兜底公式（绝不影响回复）。"""
    now = time.time()
    hour = system._hours_since_midnight(now)
    user_text = (
        f"现在 {hour:.2f} 点；疲劳 {system.fatigue:.0f}/100；"
        f"皮质醇 {system.hormones.get('cortisol', 50):.0f}、"
        f"血清素 {system.hormones.get('serotonin', 50):.0f}、"
        f"催产素 {system.hormones.get('oxytocin', 50):.0f}；"
        f"累计睡眠债 {system.sleep_debt:.1f}h。请决定今晚作息。"
    )
    raw = await _call_consolidate_llm(
        config, _PLAN_PROMPT, user_text, "hormone_sleep_plan", report=report
    )
    if raw is not None:
        parsed = _parse_emotions(raw)
        if isinstance(parsed, dict):
            b = _hour_from(parsed.get("bedtime"))
            d = _hour_from(parsed.get("duration_h"))
            if b is not None and d is not None:
                return {
                    "bedtime": b,
                    "duration_h": d,
                    "note": str(parsed.get("note", "")),
                    "source": "model",
                }
        logger.debug(f"作息计划无法解析，走兜底: {raw[:60]!r}")
        if report is not None and not report.get("error"):
            report["error"] = f"模型没给出能用的时间：{_reason(raw, 60)}"
    return _fallback_plan(system)


_NAP_PROMPT = (
    "你在替'她'决定现在要不要补个觉。现在是白天、她安静了一阵、而且有点累或欠睡。\n"
    "如果要，给一个 0.5–2 小时的小睡时长；不想睡就拒绝。睡太久会拖晚晚上入睡。\n"
    '只输出 JSON，不要解释、不要 markdown。例：{"nap":true,"dur_h":1.0,"note":"有点困"}'
)


async def _generate_nap(
    config: HormoneSystemConfig, system: HormoneSystem, report: dict | None = None
) -> dict | None:
    """用模型决定是否补觉及时长；失败返回 None（跳过，不强行补）。"""
    now = time.time()
    hour = system._hours_since_midnight(now)
    user_text = (
        f"现在 {hour:.2f} 点；疲劳 {system.fatigue:.0f}/100；"
        f"睡眠债 {system.sleep_debt:.1f}h；安静 {(now - system.last_interaction_time)/60.0:.0f} 分钟。"
    )
    raw = await _call_consolidate_llm(
        config, _NAP_PROMPT, user_text, "hormone_nap", report=report
    )
    if raw is None:
        return None
    parsed = _parse_emotions(raw)
    if isinstance(parsed, dict):
        dur = _hour_from(parsed.get("dur_h"))
        if bool(parsed.get("nap")) and dur is not None:
            return {"nap": True, "dur_h": dur, "note": str(parsed.get("note", ""))}
        return {"nap": False, "dur_h": 0.0, "note": ""}
    logger.debug(f"补觉决定无法解析: {raw[:60]!r}")
    if report is not None:
        report["error"] = f"模型没给出能用的补觉决定：{_reason(raw, 60)}"
    return None


def _dream_window_bounds(
    system: HormoneSystem, now: float, config: HormoneSystemConfig
) -> tuple[float, float] | None:
    """now 所在睡眠窗的 (bed_ts, wake_ts)；跨午夜时自动匹配今夜/明夜。"""
    cfg = config.sleep
    if not cfg.consolidate:
        return None
    bed, wake = system._resolve_window(now)
    span_h = (wake - bed) % 24.0 or 8.0
    wake_ts0 = system._day_anchor(now, wake)
    span_s = span_h * 3600.0
    for off in (0.0, 86400.0):
        wake_ts = wake_ts0 + off
        bed_ts = wake_ts - span_s
        if bed_ts <= now < wake_ts:
            return bed_ts, wake_ts
    return None


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
        先剥 KFC 的 last-mile 指令尾段（--- 分隔符后），
        再把转发信封压成「昵称：正文」（打分与入梦原料共用这一份文本）。
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

        # KFC 主动发言/自发思考这类内部提示不是他说的话：当没有消息处理
        if any(m in content for m in INTERNAL_MARKERS):
            return ""

        # 剥离 KFC last-mile 指令（"\n\n---\n" 后的行为指令部分）
        separator_idx = content.rfind("\n\n---\n")
        if separator_idx >= 0:
            content = content[:separator_idx]

        return _normalize_relay_text(content)

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
                f"{system.sleep_state_summary()} | "
                f"会话={(stream_id or 'default')[:8]}"
            )

        except Exception as exc:  # noqa: BLE001 - 异常不能影响 KFC 正常回复
            logger.warning(f"激素系统处理异常: {exc}")

        return EventDecision.SUCCESS, params


_SENT_SKIP_PREFIXES = (
    "[图片", "[表情", "[语音", "[视频", "[文件", "[json", "[CQ:", "[回复", "[合并转发",
)


def _extract_sent_text(message: Any) -> str:
    """从「已发出」的 Message 里取纯文本；非文本 / 占位内容返回空串。"""
    try:
        text = getattr(message, "processed_plain_text", "") or ""
        if not text:
            raw = getattr(message, "content", "")
            text = raw if isinstance(raw, str) else ""
        text = " ".join(str(text).split())
        if len(text) < 2:
            return ""
        if text.startswith(_SENT_SKIP_PREFIXES):
            return ""
        return text
    except Exception:  # noqa: BLE001 - 取文本失败就当没这条
        return ""


class HormoneSentHandler(BaseEventHandler):
    """把她自己发出去的文本记进梦料——梦是双方的对话，不该只有他一个人的回声。"""

    name: str = "hormone_sent_handler"
    description: str = "记录她自己的发言，供熟睡时做梦当素材"
    weight: int = 6
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_message_sent"]

    def __init__(self, plugin: Any) -> None:
        """持有插件引用即可，配置与存储在调用时再取。"""
        super().__init__(plugin)

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """只在已有状态文件的会话里追加一条她的发言；失败绝不影响发消息。"""
        try:
            config = getattr(self.plugin, "config", None)
            if not isinstance(config, HormoneSystemConfig):
                return EventDecision.SUCCESS, params
            if not config.core.enabled or not config.sleep.consolidate:
                return EventDecision.SUCCESS, params
            message = params.get("message") if isinstance(params, dict) else None
            if message is None:
                return EventDecision.SUCCESS, params
            stream_id = str(getattr(message, "stream_id", "") or "")
            text = _extract_sent_text(message)
            if not stream_id or not text:
                return EventDecision.SUCCESS, params
            store = HormoneStateStore(config.core.state_dir)
            key = store.state_path(stream_id)
            async with _lock_for(key):
                data = store.load(stream_id)
                if not data:
                    return EventDecision.SUCCESS, params  # 没状态的流不新建
                system = HormoneSystem(config)
                system.apply_state(data)
                system.record_self_turn(text)
                store.save(stream_id, system.to_state(config.core.state_version))
        except Exception as exc:  # noqa: BLE001 - 记素材失败不能影响发消息
            logger.debug(f"记录她自己的发言失败: {exc}")
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


class SleepStatusCommand(BaseCommand):
    """/睡眠状态：只读展示今晚作息、睡眠债、补觉与梦境概况（不泄露聊天原文）。"""

    name: str = "睡眠状态"
    description: str = "查看她今晚的作息/睡眠债/补觉/梦境概况（只读）"

    @cmd_route()
    async def handle_status(self) -> tuple[bool, str]:
        """读取当前 stream 的状态，拼一段人话概览。"""
        try:
            plug = getattr(self, "plugin", None)
            cfg = getattr(plug, "config", None) if plug else None
            if not isinstance(cfg, HormoneSystemConfig):
                cfg = HormoneSystemConfig()
            store = HormoneStateStore(cfg.core.state_dir)
            sid = str(getattr(self, "stream_id", "") or "")
            data = store.load(sid)
            if not data:
                return True, "（还没有她的作息记录——今晚 21:30 之后会自动定）"
            system = HormoneSystem(cfg)
            system.apply_state(data)
            return True, self._format_status(system, cfg, time.time())
        except Exception as exc:  # noqa: BLE001 - 只读命令不能抛错
            logger.warning(f"查看睡眠状态失败: {exc}")
            return True, "（状态一时没读出来，晚点再看）"

    @staticmethod
    def _format_status(
        system: HormoneSystem, config: HormoneSystemConfig, now: float
    ) -> str:
        """把 plan / 相位 / 债 / 补觉 / 梦 / 梦料 拼成几行人话（只说数量，不贴原文）。"""
        fmt = HormoneSystem._fmt_hour
        src_cn = {"model": "她自己定的", "fallback": "兜底公式定的", "config": "固定作息"}
        plan = system._resolve_plan(now)
        if isinstance(plan, dict):
            try:
                head = (f"今晚 {fmt(float(plan['bedtime']))} 睡 → "
                        f"{fmt(float(plan['wake']))} 醒（{float(plan['dur_h']):.1f}h，"
                        f"{src_cn.get(str(plan.get('source')), '自己定的')}）")
            except (KeyError, TypeError, ValueError):
                head = "今晚的作息记录有点不完整"
        else:
            head = "今晚还没定作息（默认 21:30 之后她自己定）"

        phase = system.current_sleep_phase(now)
        phase_cn = {"wind_down": "犯困收尾", "sleeping": "睡下了", "groggy": "刚睡醒"}.get(phase, "醒着")

        debt = float(getattr(system, "sleep_debt", 0.0) or 0.0)
        streak = int(getattr(system, "sleep_short_streak", 0) or 0)
        kind_cn = {"allnighter": "通宵", "short": "短睡", "normal": "正常",
                   "catchup": "补觉后", "oversleep": "睡多了"}.get(
                       str(getattr(system, "last_sleep_kind", "")), "")
        debt_s = f"睡眠债 {debt:.1f}h"
        if kind_cn:
            debt_s += f"（最近一晚：{kind_cn}"
            debt_s += f"，连续短睡 {streak} 晚）" if streak >= 2 else "）"

        nap = getattr(system, "nap", None)
        if isinstance(nap, dict):
            try:
                nap_s = f"正在补觉到 {fmt(system._hours_since_midnight(float(nap['wake_ts'])))}"
            except (KeyError, TypeError, ValueError, OSError):
                nap_s = "正在补觉"
        else:
            nap_s = "没在补觉"

        pd = getattr(system, "pending_dream", None)
        gd = getattr(system, "gen_dream", None)
        if isinstance(pd, dict):
            dream_s = "熟睡里做过一个梦，等醒来判定记不记得"
        elif isinstance(gd, dict) and gd.get("text"):
            dream_s = "记着昨晚的梦"
        else:
            dream_s = "暂时没有梦"

        turns = getattr(system, "recent_turns", []) or []
        mine = sum(1 for t in turns if str(t.get("who", "user")) == "self")
        mats = f"梦料 {len(turns)} 条（他 {len(turns) - mine} / 我 {mine}）"
        if turns:
            try:
                oldest = min(float(t.get("ts", now)) for t in turns)
                days = max(0.0, (now - oldest) / 86400.0)
                mats += f"，覆盖 {days:.1f} 天"
            except (TypeError, ValueError):
                pass

        q_cn = {"good": "良好", "short": "偏短", "bad": "没睡好"}.get(
            str(getattr(system, "sleep_quality", "")), "未知")
        score = getattr(system, "sleep_score", 0)
        return chr(10).join([
            head,
            f"现在：{phase_cn}｜{debt_s}",
            f"{nap_s}｜梦：{dream_s}",
            f"{mats}",
            f"上一夜质量：{q_cn}（{score}）",
        ])


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
        self._state_sig: dict[str, str] = {}
        self._state_ts: dict[str, float] = {}
        self._sleep_notes: dict[str, Any] = {}

    def _note_once(self, key: str, token: Any, message: str) -> bool:
        """同一件事（同一夜 / 同一个梦窗）只说一遍，别每 10 分钟刷一行。"""
        notes = getattr(self, "_sleep_notes", None)
        if not isinstance(notes, dict):
            notes = {}
            self._sleep_notes = notes
        if notes.get(key) == token:
            return False
        if len(notes) > 128:  # 长跑防泄漏：清掉重来（最多多说一句）
            notes.clear()
        notes[key] = token
        logger.info(message)
        return True

    def _should_log_state(
        self, sid: str, summ: str, now: float, config: HormoneSystemConfig
    ) -> bool:
        """[State] 日志节流：状态变化立即打，否则按 state_log_minutes 打心跳。"""
        sig_map = getattr(self, "_state_sig", None)
        if not isinstance(sig_map, dict):
            sig_map = {}
            self._state_sig = sig_map
        ts_map = getattr(self, "_state_ts", None)
        if not isinstance(ts_map, dict):
            ts_map = {}
            self._state_ts = ts_map
        prev = sig_map.get(sid)
        last = ts_map.get(sid, 0.0)
        heartbeat = float(config.sleep.state_log_minutes) * 60.0
        if prev == summ and (heartbeat <= 0.0 or now - last < heartbeat):
            return False
        sig_map[sid] = summ
        ts_map[sid] = now
        return True

    def get_components(self) -> list[type]:
        """返回插件提供的组件类列表。"""
        config = getattr(self, "config", None)
        if isinstance(config, HormoneSystemConfig) and not config.core.enabled:
            logger.info("hormone_system 已在配置中禁用")
            return []
        return [HormonePromptHandler, HormoneSentHandler, DreamReviewCommand, SleepStatusCommand]

    async def on_plugin_loaded(self) -> None:
        """加载后：若开启睡眠整理，等待调度器就绪再注册周期任务。"""
        cfg = getattr(self, "config", None)
        if not isinstance(cfg, HormoneSystemConfig):
            return
        if not (
            cfg.sleep.consolidate or cfg.sleep.dynamic_schedule or cfg.sleep.nap_enabled
        ):
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
        """等待 scheduler 启动后注册睡眠后台任务（scheduler 在 run() 才 start）。"""
        from src.kernel.scheduler import TriggerType, get_unified_scheduler

        cfg = self.config
        interval_min = min(
            int(cfg.sleep.plan_check_minutes),
            int(cfg.sleep.nap_check_minutes),
            int(cfg.sleep.dream_check_minutes),
        )
        interval = max(60, interval_min * 60)
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
                logger.info(f"hormone_system 睡眠后台任务已注册: {sid}（每 {interval // 60} 分钟看一眼：定作息 / 熟睡中段做梦 / 要不要补觉）")
                return
            except RuntimeError:
                await asyncio.sleep(0.5)
            except Exception as exc:  # noqa: BLE001 - 注册失败不影响插件其余功能
                logger.warning(f"注册梦生成任务失败: {exc}")
                return

    async def _consolidate_job(self) -> None:
        """周期任务：给各 stream 定今晚作息 / 生成当晚的梦（模型调用均在锁外）。"""
        cfg = getattr(self, "config", None)
        if not isinstance(cfg, HormoneSystemConfig):
            return
        if not (
            cfg.sleep.consolidate or cfg.sleep.dynamic_schedule or cfg.sleep.nap_enabled
        ):
            return
        store = HormoneStateStore(cfg.core.state_dir)
        now = time.time()
        if not os.path.isdir(store.state_dir):
            return
        for fn in os.listdir(store.state_dir):
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            key = store.state_path(sid)
            now = time.time()  # 每条 stream 用最新时刻，避免多流+模型耗时导致 now 过期
            try:
                # ---- 作息计划段 ----
                need_plan = False
                dropped_pd = False
                probe: HormoneSystem | None = None
                async with _lock_for(key):
                    data = store.load(sid)
                    if data:
                        probe = HormoneSystem(cfg)
                        probe.apply_state(data)
                        # 死草稿清理：>24h 还没等到醒来的梦，别一直躺在状态文件里
                        dropped_pd = probe.drop_expired_pending(now)
                        need_plan = probe.should_plan_sleep(now)
                        _summ = probe.sleep_state_summary()
                        if self._should_log_state(sid, _summ, now, cfg):
                            logger.info(f"[State] stream={sid[:8]} {_summ}")
                        if dropped_pd:
                            store.save(sid, probe.to_state(cfg.core.state_version))
                if dropped_pd:
                    self._note_once(
                        f"{sid}:pd-expired",
                        probe._today_key(now) if probe is not None else 0,
                        f"[Dream] stream={sid[:8]} 上一场梦没等到醒来确认（超过一天），草稿丢了",
                    )
                if need_plan and probe is not None:
                    plan_rep: dict = {}
                    plan_gen = await _generate_sleep_plan(cfg, probe, report=plan_rep)
                    async with _lock_for(key):
                        fresh = store.load(sid) or {}
                        psys = HormoneSystem(cfg)
                        if fresh:
                            psys.apply_state(fresh)
                        if psys._resolve_plan(now) is None:  # 二次去重
                            psys.apply_sleep_plan(
                                plan_gen["bedtime"],
                                plan_gen["duration_h"],
                                plan_gen.get("source", "fallback"),
                                plan_gen.get("note", ""),
                                now,
                            )
                            store.save(sid, psys.to_state(cfg.core.state_version))
                            logger.info(f"[Plan] stream={sid[:8]} {psys.sleep_state_summary()}")
                    if plan_gen.get("source") != "model":
                        self._note_once(
                            f"{sid}:plan",
                            probe._night_key(now),
                            f"[Plan] stream={sid[:8]} 今晚作息没问成模型，改按身体情况自己定了："
                            f"{plan_rep.get('error') or '原因不明'}",
                        )

                # ---- 补觉段 ----
                if cfg.sleep.nap_enabled and cfg.sleep.nap_per_day > 0:
                    nap_due = False
                    nap_probe: HormoneSystem | None = None
                    async with _lock_for(key):
                        data = store.load(sid)
                        if data:
                            nprobe = HormoneSystem(cfg)
                            nprobe.apply_state(data)
                            changed = False
                            if isinstance(nprobe.nap, dict) and now >= float(
                                nprobe.nap.get("wake_ts", 0.0)
                            ):
                                nprobe._finalize_nap(now)  # 到点自然醒
                                changed = True
                            nap_due = nprobe.should_nap(now)
                            if changed:
                                store.save(sid, nprobe.to_state(cfg.core.state_version))
                            nap_probe = nprobe
                    if nap_due and nap_probe is not None:
                        nap_gen = await _generate_nap(cfg, nap_probe)
                        async with _lock_for(key):
                            fresh = store.load(sid) or {}
                            nsys = HormoneSystem(cfg)
                            if fresh:
                                nsys.apply_state(fresh)
                            can_start = (
                                nsys.nap is None
                                and nsys.nap_date != nsys._today_key(now)
                            )
                            nsys.nap_check_ts = now  # 不论答应与否，冷却后再问
                            if nap_gen and nap_gen.get("nap") and can_start:
                                nsys.start_nap(
                                    float(nap_gen.get("dur_h", 1.0)), "model", now
                                )
                                logger.info(f"[Nap] stream={sid[:8]} {nsys.sleep_state_summary()}")
                            store.save(sid, nsys.to_state(cfg.core.state_version))

                # ---- 梦生成段（仅 consolidate）----
                if not cfg.sleep.consolidate:
                    continue
                items: list[dict] = []
                tone: str | None = None
                window_start = 0.0
                blocked = ""
                blocked_code = ""
                async with _lock_for(key):
                    data = store.load(sid)
                    if not data:
                        continue
                    probe2 = HormoneSystem(cfg)
                    probe2.apply_state(data)
                    bounds = _dream_window_bounds(probe2, now, cfg)
                    if bounds is None:
                        continue
                    window_start, wake_ts = bounds
                    span_s = wake_ts - window_start
                    frac = (now - window_start) / span_s
                    if not (
                        float(cfg.sleep.dream_mid_start_ratio)
                        <= frac
                        < float(cfg.sleep.dream_mid_end_ratio)
                    ):
                        continue
                    quiet = (now - probe2.last_interaction_time) / 60.0
                    onset = float(cfg.sleep.sleep_onset_gap_minutes)
                    if quiet < onset:
                        blocked = (
                            f"到该做梦的点了，但她还在聊（才安静 {int(quiet)} 分钟，"
                            f"要满 {onset:.0f} 分钟才算睡着）"
                        )
                        blocked_code = "chatting"
                    else:
                        gd = probe2.gen_dream
                        pd = probe2.pending_dream
                        if (
                            isinstance(gd, dict)
                            and float(gd.get("ts", 0.0)) >= window_start
                        ) or (
                            isinstance(pd, dict)
                            and float(pd.get("gen_ts", 0.0)) >= window_start
                        ):
                            blocked = "今晚已经做过一个梦了"  # 本窗口已生成过（含未转正草稿）
                            blocked_code = "done"
                        else:
                            items, tone = probe2.get_generation_input()
                            if not items:
                                blocked = "今天没留下能入梦的事"
                                blocked_code = "no-material"
                            elif tone in (None, "nightmare"):
                                blocked = "今天的事压成了噩梦，走潜意识阴影那条线"
                                blocked_code = "nightmare"
                if blocked:
                    # 节流键用稳定 code（不能带分钟数，否则每轮都被当成"新原因"）
                    self._note_once(
                        f"{sid}:dream",
                        (window_start, blocked_code),
                        f"[Dream] stream={sid[:8]} 今晚不做梦：{blocked}",
                    )
                    continue
                if not items or tone is None:
                    continue

                # 锁外调模型（可能耗时）→ 不占用 stream 锁、不阻塞回复
                dream_rep: dict = {}
                gen = await _generate_dream(cfg, items, tone, report=dream_rep)
                if not gen:
                    self._note_once(
                        f"{sid}:dreamfail",
                        window_start,
                        f"[Dream] stream={sid[:8]} 熟睡里想酿一个梦，没成："
                        f"{dream_rep.get('error') or '模型没给内容'}",
                    )
                    continue

                # 临界区2（短）：重载最新态 → 二次去重 → 应用 → 存
                async with _lock_for(key):
                    fresh = store.load(sid) or {}
                    system = HormoneSystem(cfg)
                    if fresh:
                        system.apply_state(fresh)
                    gd2 = system.gen_dream
                    pd2 = system.pending_dream
                    if (
                        isinstance(gd2, dict)
                        and float(gd2.get("ts", 0.0)) >= window_start
                    ) or (
                        isinstance(pd2, dict)
                        and float(pd2.get("gen_ts", 0.0)) >= window_start
                    ):
                        continue  # 等模型这段时间里可能已生成过
                    system.set_pending_dream(gen["dream"], tone)
                    store.save(sid, system.to_state(cfg.core.state_version))
                    tone_cn = {
                        "sweet": "甜", "neutral": "中性",
                        "ambivalent": "甜虐", "nightmare": "噩梦",
                    }.get(tone, tone)
                    logger.info(
                        f"[DreamPend] stream={sid[:8]} 熟睡中做了个{tone_cn}的梦，醒来再判定记不记得/值不值得记"
                    )
            except Exception as exc:  # noqa: BLE001 - 单流失败不影响其它流
                logger.warning(f"睡眠任务处理 stream {sid[:8]} 失败: {exc}")
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
