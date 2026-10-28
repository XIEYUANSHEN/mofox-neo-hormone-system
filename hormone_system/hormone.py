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
from datetime import datetime, timedelta, tzinfo
from typing import Any

try:  # 时区（stdlib，缺 tzdata 时回退本地）
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

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

# ========== 睡眠记忆整理 / 梦生成参数（§4.5 拍板） ==========
# 基调判定：R=睡前净效价，W=依恋水平
DREAM_R_NIGHTMARE = -0.15      # R ≤ 此值 → 偏负（噩梦倾向，交给 v0.2.0 阴影路径）
DREAM_R_SWEET = 0.15           # R ≥ 此值 → 偏正（美梦倾向）
DREAM_W_OXYTOCIN = 70.0        # 催产素达此视为「够在意」→ 值得梦到你
DREAM_W_CORSTRESS = 45.0       # 混合基调所需的「残存焦虑」皮质醇下限
DREAM_AMBIVALENT_PROB = 0.15   # 满足条件时，混合梦低频冒出的概率
# KFC 主动发言/自发思考这类"内部提示"不是他说的话：不评分、不进梦料。
# 只认实测见过的标记（proactive 注入的 content 里就是它）；不猜别的标签，
# 免得误伤真正在聊提示词的人。
INTERNAL_MARKERS = ("<spontaneous_thought>",)

DREAM_SELF_SALIENCE = 0.30     # 她自己说的话的固定显著性（够格入梦、不压过强情绪）
DREAM_FRAGMENT_MAX_CHARS = 620 # 整块梦料送进模型的字数上限
DREAM_RECENT_HOURS = 6.0       # 「今晚」这一层
DREAM_MID_HOURS = 24.0         # 「今天早些/昨天」这一层
# 显著性分档（|情绪分| 主导）：喂给模型时决定详略
SALIENCE_HIGH = 0.5
SALIENCE_MID = 0.25
# 模型不可用时的兜底模板（成年亲密内容允许；无数字、无激素名）
SLEEP_SWEET_DREAM_LINES = (
    "梦到你们并肩走在很安静的地方，说了好多平时没说出口的话。",
    "梦到他把你拥得很紧，暖得不想醒。",
    "梦到你们一起做饭、逛了很长的夜路，醒来嘴角还是翘的。",
    "梦到很多年后你们还在一起，画面很平常，却特别安心。",
)
SLEEP_NEUTRAL_DREAM_LINES = (
    "好像做了个梦，醒了记不太清，只剩点温吞的影子。",
    "梦里都是些零碎的日常，说不上开心也说不上难过。",
)
SLEEP_AMBIVALENT_DREAM_LINES = (
    "梦到他抱着你，很暖，可你一直怕一睁眼就没了。",
    "梦到你们去了很远的地方，很开心，却怎么都到不了终点。",
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
        # 睡眠记忆整理 / 梦生成状态
        self.recent_turns: list[dict] = []        # 仅 consolidate=true 时缓冲的原料
        self.sleep_journal: list[dict] = []       # 小本本（有界环形）
        self.gen_dream: dict | None = None        # 生成的非噩梦 {text,tone,recall,worth,ts}
        self.sleep_debt: float = 0.0              # 睡眠债（小时）
        self.sleep_short_streak: int = 0          # 连续短睡/通宵的夜数（文案区分“昨晚”与“连着几天”）
        self.sleep_awake_spans: list[list[float]] = []  # 本夜窗内“醒着”的区间（按累计安静测睡眠用）
        self.last_sleep_kind: str = ""            # 最近一晚类型：allnighter/short/normal/catchup/oversleep
        self.sleep_plan: dict | None = None       # 今晚自定作息 {night_key,bedtime,dur_h,wake,decided_at,source,note}
        self.nap: dict | None = None              # 补觉 session {start_ts,wake_ts,dur_h,source}
        self.nap_date: str = ""                   # 最近补觉归属日期（限每天次数）
        self.nap_check_ts: float = 0.0            # 上次询问是否补觉的时刻（冷却用）
        self.pending_dream: dict | None = None    # 梦草稿，晨间结算转正/丢弃
        self._state_loaded: bool = False          # 是否从磁盘载入过历史状态（首启不结算）
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
        # 睡眠债的持续影响：债没还清时抬高皮质醇底线/压低血清素上限
        self._apply_debt_bounds()

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

        # 睡眠整理原料：仅在 consolidate 开启时缓冲本轮用户消息（截断控 token）
        if self._cfg.sleep.consolidate and text:
            self._push_recent_turn(text, score)

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

    # ---------- 睡眠节律 / 时区 / 夜归属 ----------
    def _tz(self) -> tzinfo | None:
        """配置的时区；空/非法/缺 tzdata 时返回 None（用本地时区）。"""
        name = (self._cfg.sleep.timezone or "").strip()
        if not name or ZoneInfo is None:
            return None
        try:
            return ZoneInfo(name)
        except Exception:
            return None

    def _hours_since_midnight(self, ts: float) -> float:
        """时间戳转当天 0 点起的小时数（配置时区，回退本地）。"""
        dt = datetime.fromtimestamp(ts, self._tz())
        return dt.hour + dt.minute / 60.0 + dt.second / 3600.0

    def _day_anchor(self, ts: float, hour: float) -> float:
        """当天 hour 点整的时间戳（配置时区），hour 可跨午夜。"""
        dt = datetime.fromtimestamp(ts, self._tz())
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return (day_start + timedelta(hours=hour)).timestamp()

    def _night_bounds(self, now: float) -> tuple[float, float]:
        """now 所属「夜桶」的 [start_ts, end_ts]（默认 18:00 → 次日 12:00）。"""
        cfg = self._cfg.sleep
        start_h = float(cfg.night_bucket_start)
        span_h = (float(cfg.night_bucket_end) - start_h) % 24.0
        if span_h <= 0.0:
            span_h = 12.0
        hour = self._hours_since_midnight(now)
        ref = now if hour >= start_h else now - 86400.0
        start_ts = self._day_anchor(ref, start_h)
        return start_ts, start_ts + span_h * 3600.0

    def _night_key(self, now: float) -> str:
        """夜归属日期（夜桶起始日），用于 sleep_plan 匹配。"""
        start_ts, _ = self._night_bounds(now)
        return datetime.fromtimestamp(start_ts, self._tz()).date().isoformat()

    def _resolve_plan(self, now: float) -> dict | None:
        """属于当前夜的 sleep_plan；没有/不属于今夜 → None。"""
        plan = self.sleep_plan
        if not isinstance(plan, dict):
            return None
        if plan.get("night_key") != self._night_key(now):
            return None
        return plan

    def _resolve_window(self, now: float) -> tuple[float, float]:
        """返回 (bedtime, wake)：有 plan 用 plan，否则用 config 兜底。"""
        plan = self._resolve_plan(now)
        if plan:
            try:
                return float(plan["bedtime"]), float(plan["wake"])
            except (KeyError, TypeError, ValueError):
                pass
        cfg = self._cfg.sleep
        return cfg.bedtime, cfg.wake_time

    def _in_window(self, now: float, bedtime: float, wake: float) -> bool:
        """now 是否落在 [bedtime, wake)（支持跨午夜）。"""
        if abs(bedtime - wake) < 1e-6:
            return False
        hour = self._hours_since_midnight(now)
        b = bedtime % 24.0
        w = wake % 24.0
        if b < w:
            return b <= hour < w
        return hour >= b or hour < w

    def _record_awake_in_night(
        self, now: float, bedtime: float, wake: float
    ) -> None:
        """now 落在本夜窗（就寝前 0.5h ~ 起床点）内，就记一笔"醒着"。"""
        span_h = (wake - bedtime) % 24.0 or 8.0
        wake_ts0 = self._day_anchor(now, wake)
        span_s = span_h * 3600.0
        for off in (0.0, -86400.0, 86400.0):
            wake_ts = wake_ts0 + off
            bed_ts = wake_ts - span_s
            if bed_ts - 0.5 * 3600.0 <= now < wake_ts:
                self._record_awake(now)
                return

    def _record_awake(self, now: float) -> None:
        """记一笔「本夜空内醒着」的时间，并合并成区间。

        合并规则：和上一段末尾相隔 <10 分钟就算同一段清醒（一直聊=一段）。
        存区间而不是散点，长聊也不会把记录撑爆、也不会把早先的聊天弄丢。
        """
        now = float(now)
        if self.sleep_awake_spans:
            last = self.sleep_awake_spans[-1]
            if now - float(last[1]) < 600.0:
                last[1] = max(float(last[1]), now)
                last[0] = min(float(last[0]), now)
                return
        self.sleep_awake_spans.append([now, now])
        if len(self.sleep_awake_spans) > 40:
            self.sleep_awake_spans = self.sleep_awake_spans[-40:]

    def _quiet_slept(
        self, start: float, end: float, bedtime: float, wake: float
    ) -> float:
        """本夜「累计安静」的小时数（封顶 14h）。

        以前只量「最后一次互动到起床点」，于是半夜被摸一下头、亲一口就
        把整夜作废（实测：两次深夜互动 → 只算成睡了 1.1h）。现在把窗内
        醒着的时刻都记下来，累计安静段：两句话之间隔不到 10 分钟算同一段
        清醒，超过 10 分钟的静默才算睡着。
        就寝前的安静不算（除非在半小时宽限内）；终点封顶在计划起床点。
        """
        if end <= start:
            return 0.0
        wake_ts = self._day_anchor(end, wake)
        span_h = (wake - bedtime) % 24.0
        if span_h <= 0.0:
            span_h = 8.0
        bed_ts = wake_ts - span_h * 3600.0
        night_lo = bed_ts - 0.5 * 3600.0     # 允许半小时早睡宽限
        hi = min(end, wake_ts)
        if hi <= night_lo:
            return 0.0
        spans = [
            [max(night_lo, float(a)), min(hi, float(b))]
            for a, b in self.sleep_awake_spans
            if float(b) >= night_lo and float(a) <= hi
        ]
        if spans:
            # 有醒着记录：整夜累计安静（不因为最后一次互动把前面几小时作废）
            quiet = 0.0
            cursor = night_lo
            for a, b in sorted(spans):
                if a - cursor >= 600.0:
                    quiet += a - cursor
                cursor = max(cursor, b)
            if hi - cursor >= 600.0:
                quiet += hi - cursor
            return min(14.0, quiet / 3600.0)
        # 没有醒着记录（旧状态/中途重启）：退回"上次互动→起床点"
        lo = max(start, night_lo)
        if hi <= lo:
            return 0.0
        return min(14.0, (hi - lo) / 3600.0)

    def _apply_sleep(self, pre_snap: dict, pre_time: float) -> str:
        """按当前时刻结算睡眠节律，返回本轮睡眠阶段。

        v0.4：窗口优先读 sleep_plan（无则 config）；只有"她已定作息且真的睡过去
        （安静够久）"才判被吵醒；自定作息开启但今晚还没定时，不判吵醒。
        睡眠时长按夜桶内真实安静跨度测量，不再卡计划窗。
        """
        cfg = self._cfg.sleep
        now = time.time()
        hour = self._hours_since_midnight(now)

        # 补觉中：nap 期间来消息 = 被吵醒（轻）；到点自然醒
        if isinstance(self.nap, dict):
            wake_ts = float(self.nap.get("wake_ts", 0.0))
            if now < wake_ts:
                gap_min = (now - pre_time) / 60.0
                if gap_min >= cfg.sleep_onset_gap_minutes:
                    self._finalize_nap(now)
                    self._bump("cortisol", 4.0)
                    self._bump("dopamine", -3.0)
                    self.fatigue = self._clamp(self.fatigue + 2.0, 0.0, 100.0, 0.0)
                    return "nap_wake"
                return "napping"
            self._finalize_nap(now)

        plan = self._resolve_plan(now)
        dynamic = bool(cfg.dynamic_schedule)
        allow_wake = (not dynamic) or (plan is not None)
        bedtime, wake_h = self._resolve_window(now)

        # 中断标记只对「本夜」有效：上一夜被吵醒后如果整夜再没来消息（没走到结算），
        # 标记会一直留着——今晚她再被吵醒就没有激素反应，明早结算还会把昨天那笔
        # 算到今天头上（quality 与 recall 一起受罚）。
        if (
            self.sleep_interrupted_at > 0.0
            and self._night_key(self.sleep_interrupted_at) != self._night_key(now)
        ):
            self.sleep_interrupted_at = 0.0

        # 夜里来消息：睡着（安静够久）才算吵醒；一直聊 / 还没定作息 = 醒着
        # 本夜窗内（含就寝前半小时宽限）醒着的时刻都记下来：睡眠时长按累计安静算
        try:
            self._record_awake_in_night(now, bedtime, wake_h)
        except Exception:  # noqa: BLE001 - 记账失败不影响睡眠判定
            pass

        if self._in_window(now, bedtime, wake_h):
            gap_min = (now - pre_time) / 60.0
            if not allow_wake or gap_min < cfg.sleep_onset_gap_minutes:
                return ""
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

        # 跨过起床点后一次性结算昨晚睡眠（激素↔睡眠双向耦合）
        wake_ts = self._day_anchor(now, wake_h)
        if self._last_sleep_settle < wake_ts:
            self._last_sleep_settle = now
            span_h = (wake_h - bedtime) % 24.0 or 8.0
            bed_ts = wake_ts - span_h * 3600.0
            if now - pre_time > 36.0 * 3600.0 or not self._state_loaded:
                # 离线太久 / 首启无历史：没有可靠睡眠信息，只清中断标记、不结算
                self.sleep_interrupted_at = 0.0
                self.sleep_awake_spans = []
            else:
                slept = self._quiet_slept(pre_time, now, bedtime, wake_h)
                was_interrupted = self.sleep_interrupted_at > 0.0
                self.sleep_interrupted_at = 0.0
                self.sleep_awake_spans = []
                # 通宵（整夜在聊、slept≈0）也要结算：当夜有过互动即算
                if slept >= 0.5 or was_interrupted or pre_time >= bed_ts:
                    self._settle_last_sleep(pre_snap, slept, was_interrupted, now)

        # 刚睡醒的迷糊期
        if hour >= wake_h and hour - wake_h < cfg.groggy_hours:
            return "woken"
        if hour < wake_h and (hour + 24.0) - wake_h < cfg.groggy_hours:
            return "woken"

        # 睡前犯困期（支持跨午夜）
        start = bedtime - cfg.wind_down_hours
        if start < 0.0:
            if hour >= start + 24.0 or hour < bedtime:
                return "wind_down"
        elif start <= hour < bedtime:
            return "wind_down"
        return ""

    # ---------- 睡眠↔激素耦合结算辅助 ----------
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
        debt_before = self.sleep_debt

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

        # 睡眠债：分类 + 累积/自然消退 + 睡懵（夜间激素惩罚已由 quality 管线承担，不重复叠加）
        cfg_sleep = self._cfg.sleep
        target = float(cfg_sleep.target_sleep_hours)
        if cfg_sleep.sleep_debt_enabled:
            if slept < 1.0:
                kind = "allnighter"
            elif slept < target - 1.0:
                kind = "short"
            elif slept > target + float(cfg_sleep.oversleep_margin_hours):
                kind = "oversleep"
            elif slept > target + 1.5 and debt_before > 0:
                kind = "catchup"
            else:
                kind = "normal"
            self.last_sleep_kind = kind
            if kind in ("short", "allnighter"):
                self.sleep_short_streak += 1
            else:
                self.sleep_short_streak = 0
            deficit = max(0.0, target - slept)
            repay = min(self.sleep_debt, max(0.0, slept - target) * 0.7)
            self.sleep_debt = max(
                0.0,
                min(
                    float(cfg_sleep.sleep_debt_cap_hours),
                    self.sleep_debt + deficit - repay - float(cfg_sleep.debt_decay_per_night),
                ),
            )
            if kind == "oversleep":
                self._bump("dopamine", -4.0)  # 睡懵：昏沉
                self.fatigue = self._clamp(self.fatigue + 2.0, 0.0, 100.0, 0.0)
        else:
            self.last_sleep_kind = "normal"
            self.sleep_debt = 0.0
            self.sleep_short_streak = 0

        note = ""
        if self.last_sleep_kind == "allnighter":
            self.pending_dream = None  # 通宵不算有梦
        else:
            had_pending = isinstance(self.pending_dream, dict)
            recall, worth = self._judge_pending_dream(slept, was_interrupted)
            promoted = self._promote_pending_dream(recall, worth)
            if not promoted:
                # 把"这一夜没有新梦"说清楚，免得状态里那句"记着…的梦"被当成昨晚做的
                note = "｜昨晚没做梦" if not had_pending else "｜昨晚的梦没记住"

        q_cn = {"good": "良好", "short": "偏短", "bad": "没睡好"}.get(quality, quality)
        logger.info(
            f"[Sleep] 睡了 {slept:.1f}h｜质量{q_cn}（{score}）｜"
            f"被吵醒={'是' if was_interrupted else '否'}｜{self.sleep_state_summary()}｜"
            f"疲劳 {before_fatigue:.0f}→{self.fatigue:.0f}{note}"
        )

    @staticmethod
    def _fmt_hour(hour: float) -> str:
        """把 0–24 小时浮点格式化成 HH:MM。"""
        minutes = int(round(float(hour) * 60)) % (24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def sleep_state_summary(self) -> str:
        """一行人话版睡眠状态（日志用）。"""
        cfg = self._cfg.sleep
        plan = self.sleep_plan if isinstance(self.sleep_plan, dict) else None
        src_cn = {"model": "她自己定的", "fallback": "兜底公式定的", "config": "固定作息"}
        if plan:
            try:
                plan_s = (
                    f"今晚 {self._fmt_hour(float(plan.get('bedtime', 0.0)))} 睡 → "
                    f"{self._fmt_hour(float(plan.get('wake', 0.0)))} 醒"
                    f"（{float(plan.get('dur_h', 0.0)):.1f}h，"
                    f"{src_cn.get(str(plan.get('source', '')), '计划')}）"
                )
            except (TypeError, ValueError):
                plan_s = "作息计划异常"
        elif cfg.dynamic_schedule:
            plan_s = "今晚还没定作息"
        else:
            plan_s = f"固定作息 {self._fmt_hour(cfg.bedtime)}–{self._fmt_hour(cfg.wake_time)}"

        kind_cn = {
            "allnighter": "通宵", "short": "短睡", "normal": "正常",
            "catchup": "补觉", "oversleep": "睡过头",
        }.get(self.last_sleep_kind, "")
        debt_s = f"睡眠债 {self.sleep_debt:.1f}h" + (f"（{kind_cn}）" if kind_cn else "")

        nap = self.nap if isinstance(self.nap, dict) else None
        if nap:
            try:
                dt = datetime.fromtimestamp(float(nap.get("wake_ts", 0.0)), self._tz())
                nap_s = f"正在补觉到 {dt.strftime('%H:%M')}"
            except (TypeError, ValueError, OSError):
                nap_s = "正在补觉"
        else:
            nap_s = "没在补觉"

        pd = self.pending_dream
        pd_age_h = 1e9
        if isinstance(pd, dict):
            try:
                pd_age_h = (time.time() - float(pd.get("gen_ts", 0.0))) / 3600.0
            except (TypeError, ValueError):
                pd_age_h = 1e9
        if isinstance(pd, dict) and pd_age_h <= 24.0:
            dream_s = "有梦待晨间确认"
        elif isinstance(pd, dict):
            dream_s = "昨晚的梦没等到确认，已经散了"  # 草稿过期（>24h）不会再转正
        else:
            dream_s = self.dream_brief()

        q_cn = {"good": "良好", "short": "偏短", "bad": "没睡好"}.get(self.sleep_quality, self.sleep_quality)
        return "｜".join([plan_s, debt_s, nap_s, dream_s, f"睡眠质量{q_cn}（{self.sleep_score}）"])

    def dream_night_date(self) -> str:
        """最近一场记得的梦属于哪一夜（形如 2026-10-25）；没有/推不出就返回空串。

        只给事实日期，不判断"昨夜还是几夜前"——措辞交给模型（读注入的人话）和看日志的人。
        """
        gd = self.gen_dream
        if not isinstance(gd, dict) or not gd.get("text"):
            return ""
        key = str(gd.get("night_key") or "")
        if not key:
            # 旧状态没有 night_key：按梦的生成时间反推（夜桶 18:00→次日 12:00）
            try:
                key = self._night_key(float(gd.get("ts") or gd.get("gen_ts") or 0.0))
            except (TypeError, ValueError):
                return ""
        return key

    def dream_brief(self) -> str:
        """状态摘要里那一段：带日期，不做"第几夜前"的换算。"""
        key = self.dream_night_date()
        if not key:
            return "没有梦记录"
        try:
            return f"记着 {int(key[5:7])}月{int(key[8:10])}日那晚的梦"
        except (ValueError, IndexError):
            return "记着最近那晚的梦"

    def _apply_debt_bounds(self) -> None:
        """睡眠债未还清时：抬高皮质醇底线、压低血清素上限（随债下降自动收窄）。"""
        cfg = self._cfg.sleep
        if not cfg.sleep_debt_enabled or self.sleep_debt <= 0.0:
            return
        floor = 50.0 + self.sleep_debt * 0.8
        self.hormones["cortisol"] = max(
            self.hormones["cortisol"], min(100.0, floor)
        )
        ceil = 100.0 - self.sleep_debt * 0.6
        self.hormones["serotonin"] = min(
            self.hormones["serotonin"], max(0.0, ceil)
        )

    def current_sleep_phase(self, now: float | None = None) -> str:
        """纯时间判定当前相位（读 plan/config），供 prompt 每轮（含无消息回合）取用。"""
        cfg = self._cfg.sleep
        if not cfg.enabled:
            return ""
        now = now if now is not None else time.time()
        plan = self._resolve_plan(now)
        if cfg.dynamic_schedule and plan is None:
            return ""  # 今晚还没定作息
        bedtime, wake_h = self._resolve_window(now)
        if abs(bedtime - wake_h) < 1e-6:
            return ""
        hour = self._hours_since_midnight(now)
        if self._in_window(now, bedtime, wake_h):
            return "sleeping"
        if hour >= wake_h and hour - wake_h < cfg.groggy_hours:
            return "groggy"
        if hour < wake_h and (hour + 24.0) - wake_h < cfg.groggy_hours:
            return "groggy"
        start = bedtime - cfg.wind_down_hours
        if start < 0.0:
            if hour >= start + 24.0 or hour < bedtime:
                return "wind_down"
        elif start <= hour < bedtime:
            return "wind_down"
        return ""

    def should_plan_sleep(self, now: float | None = None) -> bool:
        """是否该给今晚定作息：开了自定、已过 plan_at、今夜还没有 plan。"""
        cfg = self._cfg.sleep
        if not cfg.dynamic_schedule:
            return False
        now = now if now is not None else time.time()
        if self._hours_since_midnight(now) < float(cfg.plan_at):
            return False
        return self._resolve_plan(now) is None

    def apply_sleep_plan(
        self,
        bedtime: float,
        duration_h: float,
        source: str = "model",
        note: str = "",
        now: float | None = None,
    ) -> dict:
        """校验/夹取后落地今晚作息；bedtime 支持 >24 表示跨午夜。"""
        cfg = self._cfg.sleep
        now = now if now is not None else time.time()
        earliest = float(cfg.earliest_bedtime)
        latest = float(cfg.latest_bedtime)
        raw = float(bedtime) % 24.0
        b_abs = raw + 24.0 if raw < 12.0 else raw
        b_abs = max(earliest, min(latest, b_abs))
        now_hour = self._hours_since_midnight(now)
        if now_hour >= earliest and b_abs < now_hour:
            b_abs = min(latest, now_hour + 0.25)
        dur = max(
            float(cfg.plan_sane_min_hours),
            min(float(cfg.plan_sane_max_hours), float(duration_h)),
        )
        wake = (b_abs + dur) % 24.0
        plan = {
            "night_key": self._night_key(now),
            "bedtime": round(b_abs % 24.0, 3),
            "dur_h": round(dur, 2),
            "wake": round(wake, 3),
            "decided_at": now,
            "source": source,
            "note": note,
        }
        self.sleep_plan = plan
        src_cn = {"model": "她自己定的", "fallback": "兜底公式定的", "config": "固定作息"}
        logger.info(
            f"[Plan] 今晚 {self._fmt_hour(plan['bedtime'])} 睡 {dur:.1f}h "
            f"→ {self._fmt_hour(wake)} 醒（{src_cn.get(source, source)}）"
        )
        return plan

    def _today_key(self, now: float) -> str:
        """本地日期字符串（用于每日补觉次数限制）。"""
        return datetime.fromtimestamp(now, self._tz()).date().isoformat()

    def should_nap(self, now: float | None = None) -> bool:
        """是否该考虑补觉：白天、安静够、疲劳/债超阈、今天还没补、且不侵占夜睡。"""
        cfg = self._cfg.sleep
        if not cfg.nap_enabled or self.nap is not None or cfg.nap_per_day <= 0:
            return False
        now = now if now is not None else time.time()
        if self.nap_date == self._today_key(now):
            return False
        if now - self.nap_check_ts < float(cfg.nap_recheck_minutes) * 60.0:
            return False
        bedtime, wake = self._resolve_window(now)
        if self._in_window(now, bedtime, wake):
            return False
        if (now - self.last_interaction_time) / 60.0 < float(cfg.nap_quiet_minutes):
            return False
        if (
            self.fatigue < float(cfg.nap_fatigue_threshold)
            and self.sleep_debt < float(cfg.nap_debt_threshold)
        ):
            return False
        hour = self._hours_since_midnight(now)
        b_abs = bedtime if bedtime > hour else bedtime + 24.0
        if hour + float(cfg.nap_max_hours) > b_abs:
            return False  # 会侵占夜睡
        return True

    def start_nap(
        self, duration_h: float, source: str = "model", now: float | None = None
    ) -> dict:
        """落地一次补觉 session（时长夹取到 [nap_min, nap_max]）。"""
        cfg = self._cfg.sleep
        now = now if now is not None else time.time()
        dur = max(
            float(cfg.nap_min_hours),
            min(float(cfg.nap_max_hours), float(duration_h)),
        )
        self.nap = {
            "start_ts": now,
            "wake_ts": now + dur * 3600.0,
            "dur_h": dur,
            "source": source,
        }
        self.nap_date = self._today_key(now)
        logger.info(f"[Nap] 去补觉 {dur:.1f}h（{source}）")
        return self.nap

    def _finalize_nap(self, now: float) -> None:
        """结束补觉：按实际已睡时长打折回补睡眠债、缓解疲劳。"""
        nap = self.nap
        if not isinstance(nap, dict):
            return
        start = float(nap.get("start_ts", now))
        planned = float(nap.get("dur_h", 0.0))
        actual = max(0.0, min(planned, (now - start) / 3600.0))
        if actual > 0.0:
            self.sleep_debt = max(
                0.0,
                self.sleep_debt - actual * float(self._cfg.sleep.nap_repay_factor),
            )
            self.fatigue = self._clamp(self.fatigue - actual * 8.0, 0.0, 100.0, 0.0)
        logger.info(
            f"[Nap] 醒 {actual:.1f}h | 债={self.sleep_debt:.1f}h 疲劳={self.fatigue:.0f}"
        )
        self.nap = None

    def set_pending_dream(self, dream_text: str, tone: str) -> None:
        """熟睡中段生成梦时先存草稿（内容+基调）；醒来再判定 recall/worth。"""
        now = time.time()
        self.pending_dream = {
            "text": dream_text,
            "tone": tone,
            "gen_ts": now,
            "night_key": self._night_key(now),
        }
        self.recent_turns = []

    def drop_expired_pending(self, now: float | None = None) -> bool:
        """丢掉超过 24h 还没等到结算的草稿（返回是否真的丢了）。

        草稿的过期原本只在结算那一刻判——若那个会话之后再没人说话、永远不结算，
        草稿就会一直躺在 state 里（[State] 里看着像"有梦待确认"）。后台任务每轮
        调一次这里，把死草稿清掉，状态文件才和事实一致。
        """
        pd = self.pending_dream
        if not isinstance(pd, dict):
            return False
        now = time.time() if now is None else now
        try:
            age = now - float(pd.get("gen_ts", 0.0))
        except (TypeError, ValueError):
            age = 1e9
        if age <= 24.0 * 3600.0:
            return False
        self.pending_dream = None
        return True

    def _judge_pending_dream(
        self, slept: float, was_interrupted: bool
    ) -> tuple[str, bool]:
        """醒来确定性判定记得多清楚、值不值得记（方案 A）。"""
        pd = self.pending_dream
        if not isinstance(pd, dict):
            return "none", False
        target = float(self._cfg.sleep.target_sleep_hours)
        if slept < 2.0:
            recall = "none"
        elif was_interrupted or slept < target:
            recall = "fuzzy"
        else:
            recall = "clear"
        tone = str(pd.get("tone", "neutral"))
        worth = recall != "none" and (
            recall == "clear" or tone in ("sweet", "ambivalent")
        )
        return recall, worth

    def _promote_pending_dream(self, recall: str, worth: bool) -> bool:
        """醒来判定后把草稿转正进 gen_dream / 小本本（worth 才记）；返回是否真的转正。"""
        pd = self.pending_dream
        self.pending_dream = None
        if not isinstance(pd, dict) or not pd.get("text"):
            return False
        try:
            if time.time() - float(pd.get("gen_ts", 0.0)) > 24.0 * 3600.0:
                return False  # 太久的草稿丢弃，避免隔夜误转正
        except (TypeError, ValueError):
            return False
        self.gen_dream = dict(pd)
        self.gen_dream["recall"] = recall
        self.gen_dream["worth"] = bool(worth)
        try:
            self.gen_dream["ts"] = float(pd.get("gen_ts", time.time()))
        except (TypeError, ValueError):
            self.gen_dream["ts"] = time.time()
        # 记下"这场梦属于哪一夜"，状态摘要/命令才能说清是昨晚还是几夜前（旧状态可能没有）
        if not self.gen_dream.get("night_key"):
            try:
                self.gen_dream["night_key"] = self._night_key(float(self.gen_dream["ts"]))
            except (TypeError, ValueError):
                pass
        if worth:
            now = time.time()
            self.sleep_journal.append(
                {
                    "date": self._today_key(now),
                    "text": pd["text"],
                    "tone": pd.get("tone", "neutral"),
                    "recall": recall,
                    "salience": round(self._compute_smoothed_aff(), 3),
                    "quality": self.sleep_quality,
                }
            )
            limit = self._cfg.sleep.journal_max_days
            if len(self.sleep_journal) > limit:
                self.sleep_journal = self.sleep_journal[-limit:]
        return True

    # ---------- 睡眠记忆整理 / 梦生成辅助 ----------
    def _material_cap(self) -> int:
        """梦料条数上限：兼容旧的 turn_buffer_max，取两者里大的。"""
        cfg = self._cfg.sleep
        return max(
            int(getattr(cfg, "turn_buffer_max", 40)),
            int(getattr(cfg, "dream_material_max", 120)),
        )

    def _prune_turns(self, now: float) -> None:
        """修剪梦料：超过 dream_material_days 的丢掉，超量从最早的挤掉。"""
        days = float(getattr(self._cfg.sleep, "dream_material_days", 2.0))
        cutoff = now - days * 86400.0
        kept = [t for t in self.recent_turns if float(t.get("ts", 0.0)) >= cutoff]
        self.recent_turns = kept[-self._material_cap():]

    def _push_recent_turn(
        self, text: str, score: float, who: str = "user"
    ) -> None:
        """把一句话截断后压入梦料（双方都记；仅 consolidate 开启时被调用）。"""
        if not text or not text.strip():
            return  # 只有引用壳/空内容的消息不该占一条原料
        limit = int(getattr(self._cfg.sleep, "turn_clip_chars", 120))
        clipped = len(text) > limit
        clip = text if not clipped else text[:limit] + "…"
        now = time.time()
        self.recent_turns.append(
            {
                "text": clip,
                "score": float(self._clamp(score, -1.0, 1.0, 0.0)),
                "ts": now,
                "who": "self" if who == "self" else "user",
                "clipped": clipped,
            }
        )
        self._prune_turns(now)

    def record_self_turn(self, text: str) -> None:
        """把她自己说出口的话也记进梦料（不打分，用固定显著性参与抽样）。"""
        if not self._cfg.sleep.consolidate:
            return
        self._push_recent_turn(text, 0.0, who="self")

    def _turn_salience(self, item: dict) -> float:
        """单条原料的显著性：|情绪分| 为主；她自己说的话给固定中等权重。"""
        if str(item.get("who", "user")) == "self":
            # 被截断的长回复（多半是解释/边界话）降权：半句不适合当梦料
            return DREAM_SELF_SALIENCE * (0.5 if item.get("clipped") else 1.0)
        base = abs(float(item.get("score", 0.0)))
        bonus = 0.0
        if self.hormones.get("oxytocin", 50.0) > 70:
            bonus += 0.15
        if self.hormones.get("empathy", 50.0) > 70:
            bonus += 0.10
        return base + bonus

    def _conflict_state(self, now: float) -> tuple[bool, float]:
        """最近 24h 的（是否有未消解的强负面，最负的那一分）。"""
        cfg = self._cfg.sleep
        thr = float(getattr(cfg, "dream_conflict_threshold", -0.45))
        resolve = float(getattr(cfg, "dream_resolve_threshold", 0.6))
        since = now - 24.0 * 3600.0
        recent = [
            t for t in self.recent_turns
            if float(t.get("ts", 0.0)) >= since and str(t.get("who", "user")) == "user"
        ]
        peak = 0.0
        last_bad = -1
        for i, t in enumerate(recent):
            sc = float(t.get("score", 0.0))
            peak = min(peak, sc)
            if sc <= thr:
                last_bad = i
        if last_bad < 0:
            return False, peak
        resolved = any(
            float(t.get("score", 0.0)) >= resolve for t in recent[last_bad + 1:]
        )
        return (not resolved), peak

    def get_generation_input(self) -> tuple[list[dict], str | None]:
        """抽梦料片段与梦基调，供插件层调模型生成。

        素材 = 最近 dream_material_days 天、双方说过的话。挑法：
        ① 最近 24h 的强负面必须带 dream_conflict_quota 条进梦；
        ② 按时间分层（今晚 / 今天早些 / 更早）各取最显著的一条；
        ③ 余下按显著性补齐 top_k；④ 再随机抓 dream_random_picks 条换掉最弱的非强制项；
        ⑤ 每条带前后上下文拼成「他：…／我：…」片段，片段之间打乱顺序。
        返回 (fragments, tone)，tone ∈ {'sweet','neutral','ambivalent','nightmare'} 或 None。
        有未消解的冲突时基调封顶 ambivalent（不给她做纯甜梦）；nightmare 交回阴影路径。
        """
        cfg = self._cfg.sleep
        if not cfg.consolidate or not self.recent_turns:
            return [], None
        now = time.time()
        turns = self.recent_turns
        n = len(turns)
        top_k = max(1, int(cfg.top_k))
        picked: list[int] = []
        mandatory: set[int] = set()

        def take(i: int, force: bool = False) -> None:
            if 0 <= i < n and i not in picked:
                picked.append(i)
                if force:
                    mandatory.add(i)

        # ① 冲突配额：最近 24h 最负的几条必须入梦
        quota = int(getattr(cfg, "dream_conflict_quota", 2))
        thr = float(getattr(cfg, "dream_conflict_threshold", -0.45))
        if quota > 0:
            bad = [
                (i, t) for i, t in enumerate(turns)
                if now - float(t.get("ts", 0.0)) <= 24.0 * 3600.0
                and str(t.get("who", "user")) == "user"
                and float(t.get("score", 0.0)) <= thr
            ]
            bad.sort(key=lambda it: float(it[1].get("score", 0.0)))
            for i, _ in bad[:quota]:
                take(i, force=True)

        # ② 时间分层：今晚 / 今天早些 / 更早，各取最显著的一条
        days = float(getattr(cfg, "dream_material_days", 2.0))
        bands = (
            (0.0, DREAM_RECENT_HOURS * 3600.0),
            (DREAM_RECENT_HOURS * 3600.0, DREAM_MID_HOURS * 3600.0),
            (DREAM_MID_HOURS * 3600.0, days * 86400.0),
        )
        for lo, hi in bands:
            cand = [
                (i, t) for i, t in enumerate(turns)
                if lo <= (now - float(t.get("ts", 0.0))) < hi
            ]
            if cand:
                cand.sort(key=lambda it: self._turn_salience(it[1]), reverse=True)
                take(cand[0][0])

        # ③ 显著性补齐
        for i in sorted(range(n), key=lambda j: self._turn_salience(turns[j]), reverse=True):
            if len(picked) >= top_k:
                break
            take(i)

        # ④ 随机换掉最弱的非强制项：梦要有跳跃，不能每次都是同一批
        rand_n = int(getattr(cfg, "dream_random_picks", 1))
        if rand_n > 0:
            pool = [i for i in range(n) if i not in picked]
            random.shuffle(pool)
            for i in pool[:rand_n]:
                swappable = [j for j in picked if j not in mandatory]
                if not swappable:
                    break
                weakest = min(swappable, key=lambda j: self._turn_salience(turns[j]))
                picked.remove(weakest)
                take(i)

        # ⑤ 片段化：带上下文、合并重叠，再打乱片段顺序（梦不按时间走）
        ctx = int(getattr(cfg, "dream_context_lines", 1))
        spans: list[list[int]] = []
        for i in sorted(picked):
            lo, hi = max(0, i - ctx), min(n - 1, i + ctx)
            if spans and lo <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], hi)
            else:
                spans.append([lo, hi])
        # 一个片段最多 2*ctx+2 句：相邻好几条一起被选中时，别把一整晚并成一大段
        max_len = 2 * ctx + 2
        split: list[list[int]] = []
        for lo, hi in spans:
            cur = lo
            while cur <= hi:
                split.append([cur, min(hi, cur + max_len - 1)])
                cur += max_len
        spans = split
        frags: list[dict] = []
        for lo, hi in spans:
            parts: list[str] = []
            strength = 0.0
            for j in range(lo, hi + 1):
                t = turns[j]
                prefix = "我" if str(t.get("who", "user")) == "self" else "他"
                parts.append(f"{prefix}：{t.get('text', '')}")
                strength = max(strength, abs(float(t.get("score", 0.0))))
            frags.append({"text": " ／ ".join(parts), "score": round(strength, 3)})
        random.shuffle(frags)
        out: list[dict] = []
        used = 0
        for f in frags:
            if out and used + len(f["text"]) > DREAM_FRAGMENT_MAX_CHARS:
                continue
            out.append(f)
            used += len(f["text"])

        # 基调：有未消解的冲突就不给纯甜
        R = self._compute_smoothed_aff()
        oxy = self.hormones.get("oxytocin", 50.0)
        cor = self.hormones.get("cortisol", 50.0)
        if R <= DREAM_R_NIGHTMARE:
            return out, "nightmare"
        unresolved, _peak = self._conflict_state(now)
        if unresolved and bool(getattr(cfg, "dream_sweet_needs_resolve", True)):
            return out, "ambivalent"
        if R >= DREAM_R_SWEET or oxy >= DREAM_W_OXYTOCIN:
            if (
                oxy >= DREAM_W_OXYTOCIN
                and cor >= DREAM_W_CORSTRESS
                and random.random() < DREAM_AMBIVALENT_PROB
            ):
                return out, "ambivalent"
            return out, "sweet"
        return out, "neutral"

    def apply_generated_dream(
        self, dream_text: str, tone: str, recall: str, worth: bool
    ) -> None:
        """写回模型生成的梦；worth 为真才进小本本；随后清空原料缓冲。"""
        now = time.time()
        self.gen_dream = {
            "text": dream_text,
            "tone": tone,
            "recall": recall,
            "worth": bool(worth),
            "ts": now,
        }
        if worth and dream_text:
            import datetime as _dt
            self.sleep_journal.append(
                {
                    "date": _dt.date.fromtimestamp(now).isoformat(),
                    "text": dream_text,
                    "tone": tone,
                    "recall": recall,
                    "salience": round(self._compute_smoothed_aff(), 3),
                    "quality": self.sleep_quality,
                }
            )
            limit = self._cfg.sleep.journal_max_days
            if len(self.sleep_journal) > limit:
                self.sleep_journal = self.sleep_journal[-limit:]
        self.recent_turns = []

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
            "recent_turns": [dict(t) for t in self.recent_turns],
            "sleep_journal": [dict(j) for j in self.sleep_journal],
            "gen_dream": dict(self.gen_dream) if self.gen_dream else None,
            "sleep_debt": float(self.sleep_debt),
            "sleep_short_streak": int(self.sleep_short_streak),
            "sleep_awake_spans": [[float(a), float(b)] for a, b in self.sleep_awake_spans],
            "last_sleep_kind": str(self.last_sleep_kind),
            "sleep_plan": dict(self.sleep_plan) if self.sleep_plan else None,
            "nap": dict(self.nap) if self.nap else None,
            "nap_date": str(self.nap_date),
            "nap_check_ts": float(self.nap_check_ts),
            "pending_dream": dict(self.pending_dream) if self.pending_dream else None,
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

        raw_rt = data.get("recent_turns", [])
        if isinstance(raw_rt, list):
            turns: list[dict] = []
            for item in raw_rt:
                if isinstance(item, dict) and "text" in item:
                    text = str(item.get("text", ""))
                    if any(m in text for m in INTERNAL_MARKERS):
                        continue  # 历史里混进来的系统内部提示，读盘时直接丢掉
                    who = "self" if str(item.get("who", "user")) == "self" else "user"
                    turns.append(
                        {
                            "text": str(item.get("text", "")),
                            "score": self._clamp(item.get("score", 0.0), -1.0, 1.0, 0.0),
                            "ts": float(item.get("ts", time.time())),
                            "who": who,
                            "clipped": bool(item.get("clipped", False)),
                        }
                    )
            self.recent_turns = turns[-self._material_cap():]

        raw_jn = data.get("sleep_journal", [])
        if isinstance(raw_jn, list):
            journal: list[dict] = [j for j in raw_jn if isinstance(j, dict) and "text" in j]
            self.sleep_journal = journal[-self._cfg.sleep.journal_max_days:]

        raw_gd = data.get("gen_dream")
        if isinstance(raw_gd, dict) and "text" in raw_gd:
            self.gen_dream = raw_gd

        raw_debt = data.get("sleep_debt")
        if isinstance(raw_debt, (int, float)) and not isinstance(raw_debt, bool):
            cap = self._cfg.sleep.sleep_debt_cap_hours
            self.sleep_debt = max(0.0, min(float(cap), float(raw_debt)))
        kind = data.get("last_sleep_kind")
        if isinstance(kind, str):
            self.last_sleep_kind = kind
        raw_spans = data.get("sleep_awake_spans")
        spans: list[list[float]] = []
        if isinstance(raw_spans, list):
            for it in raw_spans[-40:]:
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    try:
                        spans.append([float(it[0]), float(it[1])])
                    except (TypeError, ValueError):
                        continue
        else:
            # 兼容 0.4.2 早期写的散点字段：相邻 <10 分钟的合并成一段
            raw_pts = data.get("sleep_awake_ts")
            if isinstance(raw_pts, list):
                for x in raw_pts:
                    if isinstance(x, (int, float)) and not isinstance(x, bool):
                        p = float(x)
                        if spans and p - spans[-1][1] < 600.0:
                            spans[-1][1] = max(spans[-1][1], p)
                        else:
                            spans.append([p, p])
        self.sleep_awake_spans = spans[-40:]
        streak = data.get("sleep_short_streak")
        if isinstance(streak, int) and not isinstance(streak, bool) and streak >= 0:
            self.sleep_short_streak = min(30, streak)
        elif self.last_sleep_kind in ("short", "allnighter"):
            # 旧状态没这个字段：至少承认"昨晚就是短睡"，别让文案退回泛泛而谈
            self.sleep_short_streak = 1

        raw_plan = data.get("sleep_plan")
        if isinstance(raw_plan, dict) and raw_plan.get("night_key"):
            try:
                self.sleep_plan = {
                    "night_key": str(raw_plan["night_key"]),
                    "bedtime": float(raw_plan["bedtime"]) % 24.0,
                    "dur_h": float(raw_plan.get("dur_h", 6.0)),
                    "wake": float(raw_plan["wake"]) % 24.0,
                    "decided_at": float(raw_plan.get("decided_at", 0.0)),
                    "source": str(raw_plan.get("source", "")),
                    "note": str(raw_plan.get("note", "")),
                }
            except (KeyError, TypeError, ValueError):
                self.sleep_plan = None

        raw_nap = data.get("nap")
        if isinstance(raw_nap, dict) and "wake_ts" in raw_nap:
            try:
                self.nap = {
                    "start_ts": float(raw_nap.get("start_ts", time.time())),
                    "wake_ts": float(raw_nap["wake_ts"]),
                    "dur_h": float(raw_nap.get("dur_h", 0.0)),
                    "source": str(raw_nap.get("source", "")),
                }
            except (TypeError, ValueError):
                self.nap = None
        nd = data.get("nap_date")
        if isinstance(nd, str):
            self.nap_date = nd
        nct = data.get("nap_check_ts")
        if (
            isinstance(nct, (int, float))
            and not isinstance(nct, bool)
            and nct >= 0
        ):
            self.nap_check_ts = float(nct)

        self._state_loaded = True
        raw_pd = data.get("pending_dream")
        if isinstance(raw_pd, dict) and raw_pd.get("text"):
            self.pending_dream = raw_pd
