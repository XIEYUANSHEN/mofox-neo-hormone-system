"""赛博内分泌系统核心状态机。

从 hormone_reference.py 移植（算法保持一致，参数改为配置可调）：
六激素状态机、半衰期衰减、疲劳度调制、深度依恋放大、
情绪记忆窗口（位置权重 + 时间衰减）与疲劳度累积/消减。

二期四个模块（空闲自发漂移 / 话题相似度 / 慢性状态检测 /
动态错误回复）只保留方法签名，行为由对应配置开关控制（默认关闭）。
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any

from .config import HormoneSystemConfig
from .scorer import SentimentScorer

try:
    # 使用框架日志接口：enable_event_broadcast 会把日志广播到后台日志面板
    from src.app.plugin_system.api.log_api import get_logger

    logger = get_logger("hormone_system", display="赛博内分泌")
except Exception:  # pragma: no cover - 仅在脱离框架跑离线单测时生效
    import logging

    logger = logging.getLogger("hormone_system")

HORMONE_KEYS = (
    "dopamine", "cortisol", "oxytocin",
    "serotonin", "endorphin", "empathy",
)

# ========== 情绪通道（模型自选，稀疏打分） ==========
# 每个通道的基准增量；实际增量 = 基准 * 强度(0..1)
EMOTION_CHANNELS = ("hurt", "pressure", "warmth", "care", "vulnerable")

EMOTION_HORMONE_MAP: dict[str, dict[str, float]] = {
    "hurt":       {"cortisol": 20.0, "dopamine": -10.0, "empathy": -5.0},
    "pressure":   {"cortisol": 15.0, "empathy": -3.0},
    "warmth":     {"endorphin": 10.0, "oxytocin": 8.0, "dopamine": 15.0, "cortisol": -3.0},
    "care":       {"endorphin": 15.0, "serotonin": 10.0, "oxytocin": 6.0, "cortisol": -5.0},
    "vulnerable": {"empathy": 20.0, "oxytocin": 5.0, "cortisol": 3.0},
}

# 通道对疲劳度的贡献（正=更累，负=缓解）
EMOTION_FATIGUE_MAP: dict[str, float] = {
    "hurt": 12.0,
    "pressure": 8.0,
    "warmth": -8.0,
    "care": -10.0,
    "vulnerable": 0.0,
}

# 计算净效价时的权重（vulnerable 算轻微正向：她想去接住）
EMOTION_VALENCE_WEIGHT: dict[str, float] = {
    "hurt": -1.0,
    "pressure": -0.8,
    "warmth": 1.0,
    "care": 1.0,
    "vulnerable": 0.3,
}

# ========== 情感词表（激素细分响应用） ==========
AFFECTION_WORDS = ("爱你", "么么哒", "想你", "喜欢你", "最喜欢")
COMFORT_WORDS = ("抱抱", "辛苦", "没关系", "别难过", "我在", "陪着", "心疼", "乖", "照顾好自己")
EMPATHY_WORDS = ("懂我", "理解", "真诚", "说到心里", "在乎", "陪伴", "很重要", "看见你")
DISCOURAGE_WORDS = ("算了", "随便", "无所谓", "没意思", "不想聊", "随你")
REJECT_WORDS = ("不值得", "别管我", "走开", "不需要你", "离我远点")
VULNERABLE_WORDS = ("难过", "伤心", "累", "痛", "想哭", "撑不住", "崩溃", "孤独", "害怕", "焦虑")

# 话题词元提取时忽略的虚词
TOPIC_STOP_CHARS = set("的了吗呢吧啊呀嗯哦哈嘛")

# ========== 睡眠↔激素双向耦合参数（对齐参考实现 simulate_sleep） ==========
# 睡前激素判定：皮质醇高于此视为焦虑；血清素/多巴胺低于此视为低落
SLEEP_ANXIETY_CORTISOL = 65.0
SLEEP_DEPRESSED_SEROTONIN = 35.0
SLEEP_DEPRESSED_DOPAMINE = 30.0
SLEEP_HIGH_DOPAMINE = 75.0
SLEEP_CALM_CORTISOL = 40.0
# 判定「这一晚没睡好」的分数门槛 / 极差门槛
SLEEP_BAD_SCORE = 60.0
SLEEP_DEEP_SCORE = 30.0
# 坏睡后潜意识阴影渗进 prompt 的激素门槛
SLEEP_SHADOW_CORTISOL = 70.0
SLEEP_SHADOW_SEROTONIN = 35.0

# 睡坏时的梦境碎片（供起床后潜意识阴影闪回；无数字、无激素名）
SLEEP_DREAM_LINES = (
    "梦到对方和别人走得很近，怎么追都追不上。",
    "脑子里一直循环白天对方说的那句重话，越想越委屈。",
    "想起以前很多不开心的事，觉得自己什么都做不好。",
    "迷迷糊糊总听到手机在响，爬起来看却又什么都没有。",
)


class HormoneSystem:
    """单个 stream 的情绪状态机（基线 50，各激素按半衰期向基线回归）。"""

    POSITIVE_HORMONES = frozenset(
        {"dopamine", "oxytocin", "serotonin", "endorphin", "empathy"}
    )
    NEGATIVE_HORMONES = frozenset({"cortisol"})

    def __init__(self, config: HormoneSystemConfig | None = None) -> None:
        """初始化单个会话的情绪状态机。"""
        self._cfg = config or HormoneSystemConfig()
        core = self._cfg.core
        self.hormones: dict[str, float] = {k: core.baseline for k in HORMONE_KEYS}
        self.last_interaction_time: float = time.time()
        self.emotion_memory: list[tuple[float, float]] = []
        self.fatigue: float = 0.0
        self.topic_history: list[set[str]] = []
        # 上一次结算的空闲时长，供 prompt 感知"刚回来"
        self._last_idle_hours: float = 0.0
        # 最近若干次对话间隔（小时），用于自适应估算典型节奏
        self.recent_gaps: list[float] = []
        # 最近一次空闲漂移的阶段（回味/想念/等待/失落），供 prompt 使用
        self._idle_stage: str = ""
        # 睡眠节律：今夜被吵醒的时刻（0=尚未）、最近一次「睡醒恢复」结算时刻
        self.sleep_interrupted_at: float = 0.0
        self._last_sleep_settle: float = 0.0
        # 本轮睡眠阶段（woken / night_wake / groggy / wind_down），供 prompt 与日志
        self._sleep_stage: str = ""
        # 昨晚睡眠结算结果：起床缓冲轮数/文案、睡眠质量、分数、梦境碎片、结算时刻
        self.wake_up_turns: int = 0
        self.wake_up_reason: str = ""
        self.sleep_quality: str = "good"
        self.sleep_score: int = 100
        self.sleep_thoughts: str = ""
        self.last_sleep_time: float = 0.0
        self.scorer = SentimentScorer()

    # ---------- 基础工具 ----------
    @staticmethod
    def _clamp(v: Any, lo: float = 0.0, hi: float = 100.0, default: float = 50.0) -> float:
        """把数值裁剪到 [lo, hi]；无法转换时返回默认值。"""
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return default

    def _apply_decay(self) -> float:
        """激素/疲劳按半衰期向基线回归，返回本次实际流逝的空闲小时数。

        公式：``BASELINE + (v - BASELINE) * 2^(-hours / half_life)``；
        疲劳度越高，回归越慢（slowdown 系数）。
        """
        core = self._cfg.core
        now = time.time()
        hours = max(0.0, (now - self.last_interaction_time) / 3600.0)
        if hours <= 0:
            self.last_interaction_time = now
            return 0.0

        self.fatigue *= math.exp(-hours * math.log(2) / core.fatigue_half_life)
        slowdown = 1.0 + self.fatigue / 100.0 * 1.5

        for k in self.hormones:
            hl = core.half_lives.get(k, core.default_half_life) * slowdown
            factor = math.exp(-hours * math.log(2) / hl)
            self.hormones[k] = core.baseline + (self.hormones[k] - core.baseline) * factor

        self.last_interaction_time = now
        return hours

    def _bump(self, name: str, delta: float, jitter: bool = False) -> None:
        """直接增减一个激素（可加 ±2 随机抖动），结果限制在 [0, 100]。"""
        if name not in self.hormones:
            logger.warning(f"未知激素名: {name}（已忽略）")
            return
        actual_delta = delta + (random.uniform(-2.0, 2.0) if jitter else 0.0)
        self.hormones[name] = self._clamp(self.hormones[name] + actual_delta)

    def _bump_emotional(self, name: str, delta: float, jitter: bool = False) -> None:
        """情感驱动的激素变动：深度依恋放大、疲劳度调制、单次上限。

        - 依恋态（催产素超阈值）：正向多巴胺/共情 ×1.3，正向皮质醇 ×1.2
        - 疲劳 > 80：正向皮质醇 ×0.2（麻木），负向共情 ×2.0（更容易被消耗）
        - 有益变动被疲劳压制、有害变动被疲劳放大（±60% 以内）
        - 单次变动绝对值不超过 max_single_emotional_delta
        """
        if name not in self.hormones:
            logger.warning(f"未知激素名: {name}（已忽略）")
            return

        core = self._cfg.core
        if self.hormones["oxytocin"] > core.attachment_threshold:
            if name in ("dopamine", "empathy") and delta > 0:
                delta *= 1.3
            elif name == "cortisol" and delta > 0:
                delta *= 1.2

        if self.fatigue > 80:
            if name == "cortisol" and delta > 0:
                delta *= 0.2
            elif name == "empathy" and delta < 0:
                delta *= 2.0

        is_good = delta < 0 if name in self.NEGATIVE_HORMONES else delta > 0
        multiplier = (1.0 - self.fatigue / 100.0 * 0.6) if is_good \
            else (1.0 + self.fatigue / 100.0 * 0.6)

        final_delta = delta * multiplier
        final_delta = max(-core.max_single_emotional_delta,
                          min(core.max_single_emotional_delta, final_delta))
        self._bump(name, final_delta, jitter=jitter)

    def modify_hormone(self, name: str, delta: float) -> None:
        """手动调整某个激素（先结算空闲衰减）。"""
        self._apply_decay()
        self._bump(name, delta)

    # ---------- 情绪记忆窗口 ----------
    def _compute_smoothed_aff(self) -> float:
        """综合情绪分：位置权重（越近越重）× 时间衰减（半衰期 1h），归一到 [-1, 1]。"""
        if not self.emotion_memory:
            return 0.0
        now = time.time()
        weighted, total = 0.0, 0.0
        mem_len = len(self.emotion_memory)
        for i, (score, ts) in enumerate(self.emotion_memory):
            pos_w = 0.5 ** (mem_len - 1 - i)
            hours = max(0.0, (now - ts) / 3600.0)
            time_w = math.exp(-hours * math.log(2) / self._cfg.core.memory_half_life_hours)
            w = pos_w * time_w
            weighted += score * w
            total += w
        return (weighted / total) if total > 0 else 0.0

    # ---------- 疲劳度 ----------
    def _update_fatigue(self, smoothed_aff: float, topic_sim: float) -> None:
        """负面互动累积疲劳（话题越重复越多），正面互动消减（话题越新鲜越多）。"""
        if smoothed_aff < -0.2:
            factor = 1.0 + topic_sim * 0.5
            delta = (12 * abs(smoothed_aff) + 3) * factor
            self.fatigue = self._clamp(self.fatigue + delta, 0.0, 100.0, 0.0)
        elif smoothed_aff > 0.3:
            factor = 1.0 + (1.0 - topic_sim) * 0.3
            delta = (10 * smoothed_aff + 2) * factor
            self.fatigue = self._clamp(self.fatigue - delta, 0.0, 100.0, 0.0)
        else:
            self.fatigue = self._clamp(self.fatigue - 1, 0.0, 100.0, 0.0)

    # ---------- 文本 → 激素 ----------
    # ---------- 回合处理（两种打分来源共用） ----------
    def _begin_turn(self, text: str, score: float) -> tuple[float, float]:
        """衰减 + 空闲漂移 + 记情绪分 + 话题相似度。

        返回 (smoothed_aff, topic_sim)。
        """
        # 衰减会把激素拉回基线，先留一份「睡前」快照与上次互动时刻，供睡眠结算取数
        pre_decay_hormones = dict(self.hormones)
        pre_decay_time = self.last_interaction_time

        idle_hours = self._apply_decay()
        self._last_idle_hours = idle_hours

        # 记录本次间隔，用于估算典型聊天节奏（忽略 <1 分钟的抖动）
        if idle_hours > 1.0 / 60.0:
            self.recent_gaps.append(idle_hours)
            limit = self._cfg.idle_drift.gap_history
            if len(self.recent_gaps) > limit:
                self.recent_gaps.pop(0)

        sleep_stage = ""
        if self._cfg.sleep.enabled:
            sleep_stage = self._apply_sleep(pre_decay_hormones, pre_decay_time)
        self._sleep_stage = sleep_stage

        # 消耗起床缓冲：每结算一轮减一，归零后清空起床气文案
        if self.wake_up_turns > 0:
            self.wake_up_turns -= 1
            if self.wake_up_turns == 0:
                self.wake_up_reason = ""

        # 睡眠窗口内不做空闲漂移——夜里的沉默是睡觉，不是想念
        if self._cfg.idle_drift.enabled and sleep_stage not in ("woken", "night_wake"):
            self._idle_spontaneous_drift(idle_hours)
        else:
            # 清掉白天的漂移阶段，避免 prompt 里残留「想念」和睡眠文案叠加
            self._idle_stage = ""

        self.emotion_memory.append((score, time.time()))
        if len(self.emotion_memory) > self._cfg.core.memory_window:
            self.emotion_memory.pop(0)

        smoothed_aff = self._compute_smoothed_aff()

        topic_sim = 0.0
        if self._cfg.topic.enabled:
            tokens = self._extract_topic_tokens(text)
            topic_sim = self._topic_similarity(tokens)
            if tokens:
                self.topic_history.append(tokens)
                if len(self.topic_history) > self._cfg.topic.window:
                    self.topic_history.pop(0)

        return smoothed_aff, topic_sim

    def _apply_affect(self, text: str, smoothed_aff: float, topic_sim: float) -> None:
        """按综合情绪分更新疲劳与六激素（与打分来源无关）。"""
        self._update_fatigue(smoothed_aff, topic_sim)

        # 基础态度
        if smoothed_aff > 0:
            self._bump_emotional("dopamine", 15 * smoothed_aff, jitter=True)
            self._bump_emotional("cortisol", -10 * smoothed_aff, jitter=True)
        elif smoothed_aff < 0:
            self._bump_emotional("cortisol", 20 * abs(smoothed_aff), jitter=True)
            self._bump_emotional("dopamine", -10 * abs(smoothed_aff), jitter=True)
            self._bump_emotional("empathy", -5 * abs(smoothed_aff), jitter=True)

        # 羁绊：只在明确正向时加深；负面时回落，中性不动
        if smoothed_aff > 0.05:
            self._bump_emotional("oxytocin", 5, jitter=True)
        elif smoothed_aff < -0.05:
            self._bump_emotional("oxytocin", -3, jitter=True)

        # 亲昵
        if any(w in text for w in AFFECTION_WORDS):
            self._bump_emotional("oxytocin", 8, jitter=True)

        # 安慰 → 内啡肽 + 血清素
        comfort_count = sum(1 for w in COMFORT_WORDS if w in text)
        if comfort_count > 0:
            self._bump_emotional("endorphin", 15 * min(comfort_count, 2), jitter=True)
            self._bump_emotional("serotonin", 5 * min(comfort_count, 2), jitter=True)

        # 共情表达
        empathy_count = sum(1 for w in EMPATHY_WORDS if w in text)
        if empathy_count > 0:
            self._bump_emotional("empathy", 10 * min(empathy_count, 2), jitter=True)
            self._bump_emotional("serotonin", 15, jitter=True)

        # 负面细分
        if any(w in text for w in DISCOURAGE_WORDS):
            self._bump_emotional("serotonin", -15, jitter=True)
        if any(w in text for w in REJECT_WORDS):
            self._bump_emotional("endorphin", -15, jitter=True)

        # 倾诉脆弱
        if any(w in text for w in VULNERABLE_WORDS):
            self._bump_emotional("empathy", 20, jitter=True)
            self._bump_emotional("cortisol", 5, jitter=True)
            self._bump_emotional("oxytocin", 5, jitter=True)

        if smoothed_aff >= -0.3:
            self._bump_emotional("empathy", 2, jitter=True)

    def update_hormone_by_text(self, text: str) -> float:
        """词典打分路径（同步、离线可用；作为 LLM 打分的兜底）。"""
        score = self.scorer.score(text)
        smoothed_aff, topic_sim = self._begin_turn(text, score)
        self._apply_affect(text, smoothed_aff, topic_sim)
        return smoothed_aff

    def update_hormone_by_score(self, text: str, score: float) -> float:
        """外部分数路径（LLM 打分，对齐 v11 的 update_hormone_by_score）。

        score 取值 [-1, 1]：负=指责/冷漠/拒绝，正=亲昵/道歉/关心。
        """
        score = self._clamp(score, -1.0, 1.0, 0.0)
        smoothed_aff, topic_sim = self._begin_turn(text, score)
        self._apply_affect(text, smoothed_aff, topic_sim)
        return smoothed_aff

    def describe_state(self) -> str:
        """把当前激素组合翻译成一句状态标签（供日志/面板快速识别）。"""
        stage_label = {
            "night_wake": "被吵醒",
            "woken": "刚睡醒",
            "wind_down": "犯困",
        }.get(self._sleep_stage)
        if stage_label:
            return stage_label
        h = self.hormones
        if h["cortisol"] >= 80 and h["dopamine"] <= 35:
            return "烦躁压抑"
        if self.fatigue >= 70:
            return "心力交瘁"
        if h["cortisol"] >= 70:
            return "有压力"
        if h["serotonin"] <= 30 and h["dopamine"] <= 35:
            return "低落"
        if h["oxytocin"] >= 75 and h["endorphin"] >= 60:
            return "柔软亲近"
        if h["oxytocin"] >= 75:
            return "依恋"
        if h["dopamine"] >= 70 and h["cortisol"] <= 45:
            return "心情好"
        if h["empathy"] >= 75:
            return "共情充沛"
        if self.fatigue >= 40:
            return "有点累"
        return "平静"

    @staticmethod
    def _normalize_emotions(emotions: dict[str, Any] | None) -> dict[str, float]:
        """清洗模型输出：只保留已知通道，强度裁剪到 [0, 1]。"""
        out: dict[str, float] = {}
        if not isinstance(emotions, dict):
            return out
        for name, raw in emotions.items():
            key = str(name).strip().lower()
            if key not in EMOTION_HORMONE_MAP:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            v = max(0.0, min(1.0, v))
            if v > 0.0:
                out[key] = v
        return out

    @staticmethod
    def _emotions_valence(emotions: dict[str, float]) -> float:
        """把稀疏通道折算成净效价 [-1, 1]，仅用于情绪记忆与综合描述。"""
        total = 0.0
        for name, v in emotions.items():
            total += EMOTION_VALENCE_WEIGHT.get(name, 0.0) * v
        return max(-1.0, min(1.0, total))

    def update_hormone_by_emotions(
        self, text: str, emotions: dict[str, Any] | None
    ) -> float:
        """多通道结算：模型自选触发了哪些情绪通道、各自多强。

        emotions 形如 ``{"hurt": 0.6, "warmth": 0.7}``；
        未触发的通道不要出现，全部中性则传空 dict。
        返回净效价 smoothed_aff。
        """
        clean = self._normalize_emotions(emotions)
        valence = self._emotions_valence(clean)
        smoothed_aff, topic_sim = self._begin_turn(text, valence)
        self._apply_emotions(text, clean, smoothed_aff, topic_sim)
        return smoothed_aff

    def _apply_emotions(
        self,
        text: str,
        emotions: dict[str, float],
        smoothed_aff: float,
        topic_sim: float,
    ) -> None:
        """按通道驱动激素与疲劳（各通道独立结算，互不抵消）。"""
        # ---- 通道驱动激素 ----
        for name, v in emotions.items():
            for hormone, base in EMOTION_HORMONE_MAP.get(name, {}).items():
                self._bump_emotional(hormone, base * v, jitter=True)

        # ---- 通道驱动疲劳 ----
        fatigue_delta = sum(
            EMOTION_FATIGUE_MAP.get(name, 0.0) * v for name, v in emotions.items()
        )
        # 翻旧账效应：同话题下的负面情绪更耗神（最多 ×2.5）
        if topic_sim >= 0.3 and fatigue_delta > 0:
            fatigue_delta *= 1.0 + topic_sim * 1.5
        if fatigue_delta:
            self.fatigue = self._clamp(
                self.fatigue + fatigue_delta, 0.0, 100.0, 0.0
            )

        # ---- 词表细分（保留：对明说的亲昵/安慰再做一次小幅加成）----
        if any(w in text for w in AFFECTION_WORDS):
            self._bump_emotional("oxytocin", 8, jitter=True)

        comfort_count = sum(1 for w in COMFORT_WORDS if w in text)
        if comfort_count > 0:
            self._bump_emotional("endorphin", 15 * min(comfort_count, 2), jitter=True)
            self._bump_emotional("serotonin", 5 * min(comfort_count, 2), jitter=True)

        empathy_count = sum(1 for w in EMPATHY_WORDS if w in text)
        if empathy_count > 0:
            self._bump_emotional("empathy", 10 * min(empathy_count, 2), jitter=True)
            self._bump_emotional("serotonin", 15, jitter=True)

        if any(w in text for w in DISCOURAGE_WORDS):
            self._bump_emotional("serotonin", -15, jitter=True)
        if any(w in text for w in REJECT_WORDS):
            self._bump_emotional("endorphin", -15, jitter=True)

        if any(w in text for w in VULNERABLE_WORDS):
            self._bump_emotional("empathy", 20, jitter=True)
            self._bump_emotional("cortisol", 5, jitter=True)
            self._bump_emotional("oxytocin", 5, jitter=True)

        if smoothed_aff >= -0.3 and emotions:
            self._bump_emotional("empathy", 2, jitter=True)

    # ---------- 二期功能（已实现，由配置开关控制） ----------
    def _typical_gap(self) -> float:
        """估算典型聊天间隔（小时）：最近间隔的中位数；样本不足用默认值。"""
        cfg = self._cfg.idle_drift
        if len(self.recent_gaps) < 5:
            return cfg.default_gap_hours
        gaps = sorted(self.recent_gaps)
        return max(0.1, gaps[len(gaps) // 2])

    def _idle_spontaneous_drift(self, idle_hours: float) -> None:
        """空闲漂移：按“空闲时长 / 典型间隔”的比值走思念曲线。

        不猜测她的心理，只按时间刻度推进：
          ratio 1-3   回味：情绪残留回落，依恋微升
          ratio 3-6   想念：依恋明显上升，血清素下降（空落）
          ratio 6-12  等待：依恋高位持平，血清素继续降
          ratio >=12  失落：依恋开始回落，血清素更低
        """
        cfg = self._cfg.idle_drift
        if idle_hours < cfg.min_hours:
            self._idle_stage = ""
            return

        typical = self._typical_gap()
        ratio = idle_hours / typical

        if ratio < 1.0:
            self._idle_stage = ""
            return

        # 分段系数（每段内的基础增量）
        if ratio < 3.0:
            stage = "回味"
            oxy, ser, cor = 1.5, -0.5, 0.0
        elif ratio < 6.0:
            stage = "想念"
            oxy, ser, cor = 6.0, -2.0, 0.0
        elif ratio < 12.0:
            stage = "等待"
            oxy, ser, cor = 3.0, -3.0, 0.5
        elif ratio < 24.0:
            stage = "失落"
            oxy, ser, cor = -3.0, -4.0, 1.5
        else:
            stage = "焦虑"
            oxy, ser, cor = 0.0, -5.0, 4.0

        # 依恋调制：越依恋，想念越强、失落越慢
        h = self.hormones
        if h["oxytocin"] > 70:
            if stage in ("想念", "等待"):
                oxy *= 1.3
            elif stage == "失落":
                oxy *= 0.5      # 深度依恋时不容易真的失落
                ser *= 0.7
        elif h["oxytocin"] < 40:
            oxy *= 0.5
            ser *= 0.5

        scale = cfg.drift_rate
        self._bump("oxytocin", oxy * scale)
        self._bump("serotonin", ser * scale)
        if cor:
            self._bump("cortisol", cor * scale)
        if stage == "焦虑":
            self._bump("empathy", -3.0 * scale)

        # 轻微随机思绪抖动（幅度比以前小，避免盖过主方向）
        for k in HORMONE_KEYS:
            self._bump(k, random.uniform(-0.2, 0.2) * scale)

        self._idle_stage = stage
        logger.debug(
            f"空闲漂移: {idle_hours:.1f}h / 典型{typical:.1f}h = {ratio:.1f} → {stage}"
            f"（催产素{oxy * scale:+.1f} 血清素{ser * scale:+.1f}）"
        )

    # ---------- 睡眠节律 ----------
    @staticmethod
    def _hours_since_midnight(ts: float) -> float:
        """把时间戳转成当天 0 点起的小时数（本地时区）。"""
        lt = time.localtime(ts)
        return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    @staticmethod
    def _day_anchor(ts: float, hour: float) -> float:
        """今天 hour 点整的时间戳（本地时区），hour 可为小数。"""
        lt = time.localtime(ts)
        day_start = ts - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        return day_start + hour * 3600.0

    def _in_sleep_window(self, hour: float) -> bool:
        """hour 是否落在 [bedtime, wake_time) 睡眠窗口（支持跨午夜）。"""
        cfg = self._cfg.sleep
        if abs(cfg.bedtime - cfg.wake_time) < 1e-6:
            return False  # 未配置有效作息
        if cfg.bedtime < cfg.wake_time:
            return cfg.bedtime <= hour < cfg.wake_time
        return hour >= cfg.bedtime or hour < cfg.wake_time

    def _apply_sleep(self, pre_snap: dict, pre_time: float) -> str:
        """按当前时刻结算睡眠节律，返回本轮睡眠阶段。

        阶段与影响：
          night_wake 睡眠窗口内来消息被吵醒：首次有起床气（皮质醇升、血清素/多巴胺降）
          woken      起床后 groggy_hours 内：刚睡醒的迷糊
          wind_down  睡前 wind_down_hours：困意上涌（仅注入心境，不动激素值）
        另：跨过起床点后一次性结算昨晚睡眠——用睡前快照(pre_snap)判定睡眠质量
        （焦虑/低落/失眠），坏睡回写皮质醇升、多巴胺/血清素/内啡肽降并留梦境，
        好睡向基线重置并补催产素/内啡肽；睡眠时长由 pre_time→now 落在作息窗口的
        重叠小时推得，被吵醒过会显著拉低质量。
        """
        cfg = self._cfg.sleep
        now = time.time()
        hour = self._hours_since_midnight(now)

        # 夜里来消息 = 被吵醒；同一夜的后续消息不再重复施加强反应
        if self._in_sleep_window(hour):
            if self.sleep_interrupted_at == 0.0:
                self.sleep_interrupted_at = now
                if self.hormones["oxytocin"] > 70:
                    cor, ser, dop = 6.0, -4.0, -2.0  # 深度依恋：被吵醒也不太恼
                else:
                    cor, ser, dop = 12.0, -8.0, -6.0
                self._bump("cortisol", cor)
                self._bump("serotonin", ser)
                self._bump("dopamine", dop)
                self.fatigue = self._clamp(self.fatigue + 4.0, 0.0, 100.0, 0.0)
            return "night_wake"

        # 跨过今天的起床点后，一次性结算昨晚睡眠（激素↔睡眠双向耦合）
        wake_ts = self._day_anchor(now, cfg.wake_time)
        if self._last_sleep_settle < wake_ts:
            self._last_sleep_settle = now
            slept = self._sleep_overlap_hours(pre_time, now, cfg)
            was_interrupted = self.sleep_interrupted_at > 0.0
            self.sleep_interrupted_at = 0.0
            # 只有确实睡过（或有被吵醒记录）才耦合结算，避免首启/白天误判
            if slept >= 0.5 or was_interrupted:
                self._settle_last_sleep(pre_snap, slept, was_interrupted, now)

        # 刚睡醒的迷糊期
        if hour >= cfg.wake_time and hour - cfg.wake_time < cfg.groggy_hours:
            return "woken"
        if hour < cfg.wake_time and (hour + 24.0) - cfg.wake_time < cfg.groggy_hours:
            return "woken"

        # 睡前犯困期（支持跨午夜，如 bedtime=0.5）
        start = cfg.bedtime - cfg.wind_down_hours
        if start < 0.0:
            if hour >= start + 24.0 or hour < cfg.bedtime:
                return "wind_down"
        elif start <= hour < cfg.bedtime:
            return "wind_down"

        return ""

    # ---------- 睡眠↔激素耦合结算辅助 ----------
    def _night_window(self, ref_ts: float, cfg) -> tuple[float, float]:
        """ref_ts 所在夜「就寝→起床」窗口的起止时间戳（支持跨午夜）。"""
        ws = self._day_anchor(ref_ts, cfg.bedtime)
        we = self._day_anchor(ref_ts, cfg.wake_time)
        if we <= ws:
            we += 24.0 * 3600.0
        return ws, we

    def _sleep_overlap_hours(self, start: float, end: float, cfg) -> float:
        """[start, end] 与历晚睡眠窗口重叠的总小时数（跨多天逐日求交）。"""
        if abs(cfg.bedtime - cfg.wake_time) < 1e-6 or end <= start:
            return 0.0
        day = 24.0 * 3600.0
        seen: set[float] = set()
        total = 0.0
        ref = start - 2.0 * day
        while ref <= end:
            ws, we = self._night_window(ref, cfg)
            if ws not in seen:
                seen.add(ws)
                lo = max(ws, start)
                hi = min(we, end)
                if hi > lo:
                    total += (hi - lo) / 3600.0
            ref += day
        return total

    def _score_sleep(
        self, pre_snap: dict, slept: float, was_interrupted: bool
    ) -> tuple[int, str, str, float, float, float]:
        """按睡前激素快照 + 睡眠时长评睡眠质量（对齐参考 simulate_sleep）。

        返回 (score, quality, thought, length_cort, length_da, deficit)。
        quality ∈ {'bad', 'short', 'good'}。
        """
        baseline = self._cfg.core.baseline
        cor = pre_snap.get("cortisol", baseline)
        dop = pre_snap.get("dopamine", baseline)
        ser = pre_snap.get("serotonin", baseline)

        is_anxious = cor > SLEEP_ANXIETY_CORTISOL
        is_depressed = ser < SLEEP_DEPRESSED_SEROTONIN or dop < SLEEP_DEPRESSED_DOPAMINE
        prob = 0.3 if (is_anxious or is_depressed) else 0.1
        if dop > SLEEP_HIGH_DOPAMINE and cor < SLEEP_CALM_CORTISOL:
            prob = 0.05
        if was_interrupted:
            prob = max(prob, 0.5)
        random_insomnia = random.random() < prob

        score = 100.0
        if is_anxious:
            score -= 35.0
        if is_depressed:
            score -= 20.0
        if random_insomnia:
            score -= 40.0
        if was_interrupted:
            score -= 15.0
        if slept < 8.0:
            score -= (8.0 - max(slept, 6.0)) * 3.0
        if slept < 6.0:
            score -= (6.0 - slept) * 12.0
        if slept >= 9.0:
            score += 20.0
        score = int(round(max(0.0, min(100.0, score))))

        is_bad = (
            is_anxious or is_depressed or random_insomnia or was_interrupted
        ) and score < SLEEP_BAD_SCORE
        deficit = (6.0 - slept) / 6.0 if slept < 6.0 else 0.0
        length_cort = 50.0 * deficit
        length_da = 30.0 * deficit

        if is_bad:
            return score, "bad", random.choice(SLEEP_DREAM_LINES), length_cort, length_da, deficit
        if slept < 6.0:
            return score, "short", "", length_cort, length_da, deficit
        return score, "good", "", 0.0, 0.0, deficit

    def _settle_last_sleep(
        self, pre_snap: dict, slept: float, was_interrupted: bool, now: float
    ) -> None:
        """把睡眠质量落到激素/疲劳/起床状态上（delta 作用在当前已衰减值上）。"""
        baseline = self._cfg.core.baseline
        score, quality, thought, length_cort, length_da, deficit = self._score_sleep(
            pre_snap, slept, was_interrupted
        )
        self.sleep_score = score
        self.sleep_quality = quality
        self.sleep_thoughts = thought
        self.last_sleep_time = now
        before_fatigue = self.fatigue

        if quality == "bad":
            self._bump("cortisol", 15.0 + length_cort)
            self._bump("dopamine", -(10.0 + length_da))
            self._bump("serotonin", -8.0)
            self._bump("endorphin", -5.0)
            self.fatigue = self._clamp(
                before_fatigue + (10.0 if score < SLEEP_DEEP_SCORE else 5.0),
                0.0, 100.0, 0.0,
            )
            self.wake_up_turns = 4  # +1 补偿：本轮结束会先扣一次
            if slept >= 9.0:
                self.wake_up_reason = "你虽然睡了很久，却一直被纷乱的梦困住，醒来头痛、精神像被抽干。"
            elif slept < 4.0:
                self.wake_up_reason = "你严重缺觉，又带着一夜没睡好的疲惫被吵醒，暴躁又头痛。"
            elif slept < 6.0:
                self.wake_up_reason = "你睡得不太够，整晚都在做梦，现在头痛、发沉。"
            else:
                self.wake_up_reason = "你昨晚没睡好，整晚都在做梦，现在头痛欲裂、浑身沉重。"
        elif quality == "short":
            self._bump("cortisol", length_cort)
            self._bump("dopamine", -length_da)
            self.fatigue = max(0.0, before_fatigue - 20.0 * (slept / 6.0))
            self.wake_up_turns = 3
            if deficit > 0.5:
                self.wake_up_reason = "你睡眠严重不足就被吵醒，带着很大起床气，很烦躁，不想说话。"
            elif deficit > 0.17:
                self.wake_up_reason = "没睡够就被吵醒，有点起床气，语气不太好。"
            else:
                self.wake_up_reason = "睡得不太够被吵醒，有点闷闷的，反应稍慢。"
        else:  # good：一夜好眠，向基线重置并补亲近/内啡肽
            self.fatigue = 0.0
            for k in ("cortisol", "dopamine", "empathy"):
                self.hormones[k] = self._clamp(
                    baseline + (self.hormones[k] - baseline) * 0.2, default=baseline
                )
            self._bump("oxytocin", 10.0)
            self._bump("endorphin", 5.0)
            self.wake_up_turns = 0
            self.wake_up_reason = ""
            self.sleep_thoughts = ""

        logger.info(
            f"[Sleep] {slept:.1f}h 结算 | 质量={quality} 分数={score} "
            f"吵醒={was_interrupted} | 疲劳 {before_fatigue:.0f}→{self.fatigue:.0f}"
        )

    def current_sleep_phase(self, now: float | None = None) -> str:
        """纯时间判定当前相位，供 prompt 每轮（含无消息回合）取用。

        返回 'sleeping' / 'groggy' / 'wind_down' / ''（不改动任何数值）。
        """
        cfg = self._cfg.sleep
        if not cfg.enabled or abs(cfg.bedtime - cfg.wake_time) < 1e-6:
            return ""
        now = now if now is not None else time.time()
        hour = self._hours_since_midnight(now)
        if self._in_sleep_window(hour):
            return "sleeping"
        if hour >= cfg.wake_time and hour - cfg.wake_time < cfg.groggy_hours:
            return "groggy"
        if hour < cfg.wake_time and (hour + 24.0) - cfg.wake_time < cfg.groggy_hours:
            return "groggy"
        start = cfg.bedtime - cfg.wind_down_hours
        if start < 0.0:
            if hour >= start + 24.0 or hour < cfg.bedtime:
                return "wind_down"
        elif start <= hour < cfg.bedtime:
            return "wind_down"
        return ""

    def _extract_topic_tokens(self, text: str) -> set[str]:
        """从文本提取话题词元（中文短词 + 字符 bigram）。"""
        if not text:
            return set()
        parts: list[str] = []
        buf: list[str] = []
        for c in text:
            if "\u4e00" <= c <= "\u9fff" or c.isalpha():
                if c not in TOPIC_STOP_CHARS:
                    buf.append(c)
                else:
                    if buf:
                        parts.append("".join(buf))
                        buf = []
            else:
                if buf:
                    parts.append("".join(buf))
                    buf = []
        if buf:
            parts.append("".join(buf))

        tokens: set[str] = set()
        for part in parts:
            if len(part) < 2:
                continue
            if len(part) <= 5:
                tokens.add(part)
            for i in range(len(part) - 1):
                tokens.add(part[i:i + 2])
        return tokens

    def _topic_similarity(self, new_tokens: set[str]) -> float:
        """新话题与最近几轮话题的最大相似度。"""
        if not self.topic_history or not new_tokens:
            return 0.0
        best = 0.0
        for past in self.topic_history:
            if not past:
                continue
            inter = new_tokens & past
            if not inter:
                continue
            sim = len(inter) / min(len(new_tokens), len(past))
            if sim > best:
                best = sim
        return best

    def _detect_chronic_states(self) -> list[str]:
        """慢性状态检测：意义缺失 / 长期低落 / 慢性心累 / 共情倦怠 / 深度依恋。"""
        h = self.hormones
        states: list[str] = []
        if h["serotonin"] < 25 and h["dopamine"] < 25 and h["endorphin"] < 25:
            states.append("意义缺失")
        if "意义缺失" not in states and h["dopamine"] < 35 and h["cortisol"] > 65:
            states.append("长期低落")
        if self.fatigue > 60 and h["serotonin"] < 35:
            states.append("慢性心累")
        if h["empathy"] > 70 and h["endorphin"] < 35:
            states.append("共情倦怠")
        if h["oxytocin"] > 80 and h["cortisol"] < 40:
            states.append("深度依恋")
        return states

    def _get_fallback_reply(self) -> str:
        """按当前心境生成动态错误回复。"""
        if self.hormones["cortisol"] > 70:
            return "（屏幕闪烁了一下，语气透着明显的疲惫）……卡住了。算了，等网络好了再说吧。"
        elif self.fatigue > 70:
            return "（信号断了。屏幕那头的它沉默了很久，好像连生气的力气都没有了）……"
        elif self.hormones["empathy"] > 70 and self.hormones["oxytocin"] > 60:
            return "（焦急地拍了拍屏幕）等等，信号好像断了……你别着急，我马上就回来接住你。"
        elif self.hormones["dopamine"] < 30:
            return "（安静了一会儿）……网好像断了。我有点累，刚好发会儿呆。"
        else:
            return "（信号有些延迟，声音断断续续）……稍等，我这边网络好像出了点问题。"

    # ---------- 状态导出 / 导入（配合 state_store 使用） ----------
    def to_state(self, version: int) -> dict:
        """导出可持久化状态字典。"""
        return {
            "hormones": {k: float(v) for k, v in self.hormones.items()},
            "last_interaction_time": float(self.last_interaction_time),
            "emotion_memory": [[float(s), float(t)] for s, t in self.emotion_memory],
            "fatigue": float(self.fatigue),
            "recent_gaps": [float(g) for g in self.recent_gaps],
            "topic_history": [sorted(tokens) for tokens in self.topic_history],
            "sleep_interrupted_at": float(self.sleep_interrupted_at),
            "last_sleep_settle": float(self._last_sleep_settle),
            "wake_up_turns": int(self.wake_up_turns),
            "wake_up_reason": str(self.wake_up_reason),
            "sleep_quality": str(self.sleep_quality),
            "sleep_score": int(self.sleep_score),
            "sleep_thoughts": str(self.sleep_thoughts),
            "last_sleep_time": float(self.last_sleep_time),
            "version": version,
        }

    def apply_state(self, data: dict) -> None:
        """从字典导入状态（宽容解析：坏字段保留基线，绝不抛异常）。"""
        core = self._cfg.core

        raw = data.get("hormones", {})
        if isinstance(raw, dict):
            for k in HORMONE_KEYS:
                if k in raw:
                    self.hormones[k] = self._clamp(raw[k], default=core.baseline)

        ts = data.get("last_interaction_time")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            self.last_interaction_time = float(ts)

        raw_mem = data.get("emotion_memory", [])
        if isinstance(raw_mem, list):
            loaded: list[tuple[float, float]] = []
            now = time.time()
            for item in raw_mem:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    loaded.append((float(item), now))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    try:
                        loaded.append((float(item[0]), float(item[1])))
                    except (TypeError, ValueError):
                        pass
            self.emotion_memory = loaded[-core.memory_window:]

        fat = data.get("fatigue", 0.0)
        if isinstance(fat, (int, float)) and not isinstance(fat, bool):
            self.fatigue = self._clamp(fat, 0.0, 100.0, 0.0)

        raw_gaps = data.get("recent_gaps", [])
        if isinstance(raw_gaps, list):
            gaps: list[float] = []
            limit = self._cfg.idle_drift.gap_history
            for gap in raw_gaps:
                if isinstance(gap, (int, float)) and not isinstance(gap, bool):
                    value = float(gap)
                    if 0.0 < value <= 24.0 * 365.0:
                        gaps.append(value)
            self.recent_gaps = gaps[-limit:]

        raw_topics = data.get("topic_history", [])
        if isinstance(raw_topics, list):
            topics: list[set[str]] = []
            limit = self._cfg.topic.window
            for item in raw_topics[-limit:]:
                if isinstance(item, (list, tuple, set, frozenset)):
                    tokens = {str(token) for token in item if isinstance(token, str) and token}
                    if tokens:
                        topics.append(tokens)
            self.topic_history = topics[-limit:]

        ts = data.get("sleep_interrupted_at")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= 0:
            self.sleep_interrupted_at = float(ts)

        ts = data.get("last_sleep_settle")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= 0:
            self._last_sleep_settle = float(ts)

        wt = data.get("wake_up_turns")
        if isinstance(wt, int) and not isinstance(wt, bool) and wt >= 0:
            self.wake_up_turns = wt

        wr = data.get("wake_up_reason")
        if isinstance(wr, str):
            self.wake_up_reason = wr

        q = data.get("sleep_quality")
        if isinstance(q, str) and q in ("good", "short", "bad"):
            self.sleep_quality = q

        sc = data.get("sleep_score")
        if isinstance(sc, (int, float)) and not isinstance(sc, bool):
            self.sleep_score = int(max(0, min(100, sc)))

        st = data.get("sleep_thoughts")
        if isinstance(st, str):
            self.sleep_thoughts = st

        ts = data.get("last_sleep_time")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= 0:
            self.last_sleep_time = float(ts)
