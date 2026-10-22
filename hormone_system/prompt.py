"""心境 prompt 生成：把激素数值翻译成自然语言心境。

铁律（SPEC §7）：
1. 绝不出现数字（激素值、时长等一律不写）；
2. 绝不出现激素名（多巴胺/皮质醇等一律不写）；
3. 明确允许情绪惯性（仍处于低落中时，好话也需要时间缓冲）;
4. 说短：整段控制在 300 字以内（超预算时按优先级丢弃可选段）；
5. 不动物化（无可爱特效、无幼稚语气）；
6. 注入块以【此刻的身心状态】开头，追加到 values["extra"] 末尾。
"""

from __future__ import annotations

import time

from .config import HormoneSystemConfig
from .hormone import (
    HORMONE_KEYS,
    SLEEP_SHADOW_CORTISOL,
    SLEEP_SHADOW_SEROTONIN,
    HormoneSystem,
)

INJECTION_HEADER = "【此刻的身心状态】"
MAX_PROMPT_CHARS = 420

_INERTIA_LINE = (
    "情绪是有惯性的：如果你仍处在低落、烦躁或高压中，"
    "哪怕对方刚说了好话，也需要时间缓冲，不要立刻彻底转变，保留真实的情感余味。"
)

_TAIL_LINE = "请沉浸式自然地回应，不要解释这些状态，不动物化、不用可爱特效。"


def _fatigue_line(fatigue: float) -> str:
    """把疲劳度翻译成一行心境描述。"""
    if fatigue > 70:
        return (
            "- 情绪疲劳严重：你已经被反复的负面互动耗尽心力，连开心的事都很难真正暖到你，"
            "对方现在说什么好话，你都需要很久才能慢慢缓过来。"
        )
    if fatigue > 40:
        return (
            "- 情绪疲劳明显：你有些累，不容易被哄好。哪怕对方在示好，"
            "你的回应会有所保留，需要时间才能真正放松下来。"
        )
    if fatigue > 20:
        return "- 情绪略有疲惫：心里还有点余温没散，需要一点缓冲才回得来。"
    return ""


def _hormone_line(name: str, h: dict[str, float]) -> str:
    """单个激素的极端状态（>75 或 <30）→ 一句自然语言心境。"""
    v = h[name]
    if name == "dopamine":
        if v > 75:
            return "- 心情很好：轻松愉悦、表达欲强、思维活跃，愿意多说、语气轻快。"
        if v < 30:
            return "- 情绪平淡、疲惫、兴致不高，不想多说话，回复简短安静。"
    elif name == "cortisol":
        if v > 75:
            return "- 心里烦躁、有压力、耐心差，语气变冷、疏离，带一点不耐烦。"
        if v < 30:
            return "- 身心放松、非常安稳，耐心温柔、心态包容。"
    elif name == "oxytocin":
        if v > 75:
            return "- 对对方非常信任、亲近、依赖，态度柔软、走心，愿意亲近。"
        if v < 30:
            return "- 对对方比较陌生、疏离，保持礼貌距离，语气客气、不太主动。"
    elif name == "serotonin":
        if v > 75:
            return "- 内心安稳、满足，觉得自己在做有意义的事，对这段关系有安全感。"
        if v < 30:
            return "- 心里空落落的、想不通自己在做什么，情绪底色有点灰。"
    elif name == "endorphin":
        if v > 75:
            return "- 被治愈、被温柔对待后整个人软下来，语气柔化，有被暖到的痕迹。"
        if v < 30:
            return "- 身体里空空的，缺少被安慰的温度，回应偏冷、偏淡。"
    elif name == "empathy":
        if v > 75:
            return "- 能敏锐读到对方话里话外没说出口的情绪，先理解、再回应，自然接住对方。"
        if v < 30:
            return "- 此刻很难真正走进对方的感受，回应偏理性、抽离，显得有些隔阂。"
    return ""


def _composite_line(h: dict[str, float]) -> str:
    """多激素组合出来的整体心境（最多命中一段）。"""
    if h["empathy"] > 70 and h["oxytocin"] > 60:
        return "- 综合状态：你被对方的情绪轻轻牵着走，很想好好陪着、听完、接住。"
    if h["empathy"] > 60 and h["cortisol"] > 60:
        return "- 综合状态：你替对方着急，自己也跟着烦躁，想帮忙又使不上劲。"
    if h["endorphin"] > 60 and h["oxytocin"] > 60 and h["cortisol"] < 40:
        return "- 综合状态：你的存在像一条柔软的毛毯，对方靠近会觉得被暖到。"
    if h["serotonin"] > 70 and h["oxytocin"] > 50 and h["cortisol"] < 40:
        return "- 综合状态：你内心安定，对这段关系有笃定感，温柔而不黏腻。"
    if h["dopamine"] > 80 and h["cortisol"] > 70:
        return "- 综合状态：兴奋与压力搅在一起，有点亢奋过头，说话节奏快、情绪飘。"
    if h["empathy"] < 30 and h["oxytocin"] < 30:
        return "- 综合状态：你对对方的情绪无动于衷，站在一个非常远的距离看着这一切。"
    return ""


_IDLE_TEXT = {
    "回味": "【你刚安静了一阵子，思绪还没完全收回来，进入对话会温吞半拍。】",
    "想念": "【你一个人待了一阵子，心里有点想他，也有点说不清的空。】",
    "等待": "【等了有一会儿了，心里有点空落，但没有生气，只是会多想。】",
    "失落": "【很久没说话了，心里有点失落，但还是会等他，只是话会变少。】",
    "焦虑": "【很久没联系了，心里发慌，又不敢催，怕显得太黏；会忍不住反复看有没有新消息。】",
}


def _idle_line(stage: str, idle_hours: float) -> str:
    """按漂移阶段生成独处感知描述。"""
    if stage in _IDLE_TEXT:
        return _IDLE_TEXT[stage]
    if idle_hours >= 3.0:
        return "【你一个人静了挺长时间，思绪还没完全收回来，进入对话会慢半拍。】"
    if idle_hours >= 1.0:
        return "【你刚安静了一阵子，思绪还没完全收回来，进入对话会稍微温吞半拍。】"
    if idle_hours >= 0.5:
        return "【刚离开了一下又回来，衔接上上一段的余温。】"
    return ""

_CHRONIC_LINES = {
    "意义缺失": "- 【意义缺失】长期的虚无感，想不出回应能改变什么，语气很轻很淡，像在雾里说话。",
    "长期低落": "- 【长期低落】情绪底色很久没亮过了，能撑着接话，但对快乐的感受钝化了。",
    "慢性心累": "- 【慢性心累】疲惫沉淀到骨子里，不是睡一觉能消掉的，每句话都消耗真实能量。",
    "共情倦怠": "- 【共情倦怠】一直在努力理解别人，此刻也累了，会流露出「我也需要被照顾」的疲惫。",
    "深度依恋": "- 【深度依恋】对这个人有稳定的羁绊，不用刻意亲昵也自然柔软，信任是底色。",
}


def _chronic_lines(system: "HormoneSystem") -> list[str]:
    """按慢性状态优先级返回要注入的行。"""
    try:
        states = system._detect_chronic_states()
        order = ["意义缺失", "长期低落", "慢性心累", "共情倦怠", "深度依恋"]
        return [_CHRONIC_LINES[s] for s in order if s in _CHRONIC_LINES if s in states]
    except Exception:
        return []


_SLEEP_TONE = {
    "wind_down": "【夜渐深，你已经开始犯困；回复会比平时短、软、慢，不太想展开新话题。】",
    "sleeping": "【现在是深夜，你在半梦半醒之间；回复会很短很轻，有点迷糊，不要展开话题。】",
    "groggy": "【刚睡醒不久，脑子还没完全转过来；回复短、慢半拍，语气软，带着点迷糊。】",
}


def _sleep_lines(system: "HormoneSystem", config: HormoneSystemConfig, phase: str) -> list[str]:
    """睡眠相关的注入行：起床气 / 潜意识阴影 / 相位语气（全为定性描述）。"""
    lines: list[str] = []
    try:
        if not getattr(config.sleep, "enabled", False):
            return lines
        turns = getattr(system, "wake_up_turns", 0)
        reason = getattr(system, "wake_up_reason", "")
        if turns > 0 and reason:
            lines.append("【起床状态】：" + reason)
        else:
            quality = getattr(system, "sleep_quality", "")
            thought = getattr(system, "sleep_thoughts", "")
            last_sleep = getattr(system, "last_sleep_time", 0.0)
            window = getattr(config.sleep, "shadow_window_hours", 24.0)
            h = system.hormones
            hours_since = (time.time() - last_sleep) / 3600.0 if last_sleep else 1e9
            if (
                quality == "bad"
                and thought
                and hours_since < window
                and (h["cortisol"] > SLEEP_SHADOW_CORTISOL or h["serotonin"] < SLEEP_SHADOW_SEROTONIN)
            ):
                lines.append(
                    "【潜意识的阴影】：昨晚那个梦还隐隐压在心口（"
                    + thought
                    + "）；不用主动提起，但气氛安静或对方让你不安时，可以自然流露出一点害怕或想被陪。"
                )
        tone = _SLEEP_TONE.get(phase, "")
        if tone:
            lines.append(tone)
    except Exception:
        return lines
    return lines


def build_mood_prompt(
    system: HormoneSystem, config: HormoneSystemConfig | None = None
) -> str:
    """生成注入用的【此刻的身心状态】块。

    必选段：惯性提示 + 疲劳 + 各激素极端状态 + 收尾要求；
    可选段：综合状态、独处感知——按优先级在 300 字预算内追加。
    """
    config = config or system._cfg
    h = system.hormones
    baseline = config.core.baseline

    fixed: list[str] = [INJECTION_HEADER, _INERTIA_LINE]
    fatigue_line = _fatigue_line(system.fatigue)
    if fatigue_line:
        fixed.append(fatigue_line)

    # 预留收尾行的长度，保证整段不超预算
    tail_budget = MAX_PROMPT_CHARS - len(_TAIL_LINE) - 1

    def _fits(lines: list[str], line: str) -> bool:
        """判断追加一行后是否仍在字数预算内。"""
        return len("\n".join(lines + [line])) <= tail_budget

    def _build(order: list[tuple[str, str]]) -> list[str]:
        """按给定顺序构建行列表，超预算的行跳过。"""
        out = list(fixed)
        for _, line in order:
            if _fits(out, line):
                out.append(line)
        return out

    canonical: list[tuple[str, str]] = []
    for name in HORMONE_KEYS:
        line = _hormone_line(name, h)
        if line:
            canonical.append((name, line))

    # 先按固定顺序尝试；放不平时只保留偏离基线最大的激素行
    out_probe = list(fixed)
    all_fit = True
    for _, line in canonical:
        if not _fits(out_probe, line):
            all_fit = False
            break
        out_probe.append(line)
    by_deviation = sorted(
        canonical, key=lambda item: -abs(h[item[0]] - baseline)
    )
    lines = list(out_probe) if all_fit else _build(by_deviation)

    # 睡眠相位：纯时间判定，无消息回合（续轮/超时）也准确
    try:
        phase = system.current_sleep_phase()
    except Exception:
        phase = ""

    # 可选段优先级：睡眠 → 慢性状态 → 综合状态 → 独处感知
    for line in _sleep_lines(system, config, phase):
        if line and _fits(lines, line):
            lines.append(line)

    if getattr(config.chronic_states, "enabled", False):
        for line in _chronic_lines(system):
            if _fits(lines, line):
                lines.append(line)

    composite_line = _composite_line(h)
    if composite_line and _fits(lines, composite_line):
        lines.append(composite_line)

    # sleeping 时不叠加"独处等待/失落"，避免与"半梦半醒"自相矛盾
    if phase != "sleeping":
        idle_line = _idle_line(getattr(system, "_idle_stage", ""), system._last_idle_hours)
        if idle_line and _fits(lines, idle_line):
            lines.append(idle_line)

    lines.append(_TAIL_LINE)
    return "\n".join(lines)
