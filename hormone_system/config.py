"""赛博内分泌系统（hormone_system）插件配置。

第一版只启用 core 节（六激素、半衰期衰减、情感打分、情绪记忆窗口、
疲劳度、心境 prompt）。二期四个模块（idle_drift / topic /
chronic_states / fallback_reply）仅预留配置开关，默认全部关闭。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class HormoneSystemConfig(BaseConfig):
    """赛博内分泌系统（hormone_system）配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "赛博内分泌系统：为 KFC 角色提供情绪惯性与疲劳度"

    @config_section("core")
    class CoreSection(SectionBase):
        """核心配置：六激素、半衰期衰减、情绪记忆窗口、疲劳度。"""

        enabled: bool = Field(
            default=True,
            description="是否启用激素系统（总开关）",
        )
        state_dir: str = Field(
            default="data/plugins/hormone_system",
            description="状态存储目录（相对 bot 根目录），按 stream_id 隔离",
        )
        baseline: float = Field(
            default=50.0,
            description="激素基线值（所有激素的中性位置）",
        )
        half_lives: dict[str, float] = Field(
            default_factory=lambda: {
                "dopamine": 12.0,
                "cortisol": 6.0,
                "oxytocin": 72.0,
                "serotonin": 48.0,
                "endorphin": 4.0,
                "empathy": 24.0,
            },
            description="各激素半衰期（小时），空闲多久偏差减半",
        )
        default_half_life: float = Field(
            default=24.0,
            description="未知激素的默认半衰期（小时）",
        )
        fatigue_half_life: float = Field(
            default=12.0,
            description="疲劳度半衰期（小时）",
        )
        memory_half_life_hours: float = Field(
            default=1.0,
            description="情绪记忆窗口的时间衰减半衰期（小时）",
        )
        max_single_emotional_delta: float = Field(
            default=25.0,
            description="单次情感驱动的激素变动上限（绝对值）",
        )
        attachment_threshold: float = Field(
            default=80.0,
            description="催产素超过该值进入深度依恋态，放大情感增益",
        )
        memory_window: int = Field(
            default=8,
            ge=1,
            le=20,
            description="情绪记忆窗口保留的最近情绪分数条数",
        )
        max_history: int = Field(
            default=40,
            ge=1,
            description="消息历史上限（预留参数，第一版未使用）",
        )
        state_version: int = Field(
            default=2,
            description="状态文件版本号",
        )

    @config_section("idle_drift")
    class IdleDriftSection(SectionBase):
        """二期：空闲自发情绪漂移（接口预留，默认关闭）。"""

        enabled: bool = Field(
            default=True,
            description="是否启用「IdleDriftSection」模块",
        )
        min_hours: float = Field(
            default=0.5,
            description="至少空闲多少小时才触发自发波动",
        )
        max_compensation_hours: float = Field(
            default=6.0,
            description="单次空闲最多补偿多少小时（避免长时间不聊飘太远）",
        )
        drift_rate: float = Field(
            default=1.0,
            description="基础漂移速率（幅度倍率）",
        )
        default_gap_hours: float = Field(
            default=6.0,
            description="数据不足时假设的典型聊天间隔（小时）",
        )
        gap_history: int = Field(
            default=20,
            ge=5,
            le=200,
            description="用于估算典型间隔的最近间隔样本数",
        )

    @config_section("sleep")
    class SleepSection(SectionBase):
        """睡眠节律：按作息施加犯困/被吵醒/睡饱的影响（夜间沉默不再算想念）。"""

        enabled: bool = Field(
            default=True,
            description="是否启用睡眠节律模块",
        )
        bedtime: float = Field(
            default=23.5,
            ge=0.0,
            le=24.0,
            description="就寝时间（小时，23.5 = 23:30；与 wake_time 相等视为不睡眠）",
        )
        wake_time: float = Field(
            default=7.0,
            ge=0.0,
            le=24.0,
            description="起床时间（小时）",
        )
        wind_down_hours: float = Field(
            default=1.5,
            ge=0.0,
            le=6.0,
            description="睡前多少小时开始犯困",
        )
        groggy_hours: float = Field(
            default=1.0,
            ge=0.0,
            le=4.0,
            description="起床后多少小时内算「刚睡醒」",
        )
        shadow_window_hours: float = Field(
            default=24.0,
            ge=0.0,
            le=72.0,
            description="睡坏后多少小时内、且压力高/情绪低时，梦境会以「潜意识的阴影」渗进心境",
        )
        consolidate: bool = Field(
            default=True,
            description="睡眠记忆整理/梦生成总开关（默认开；关=只数值衰减，不注册调度、不写小本本、不调模型）",
        )
        consolidate_model_task: str = Field(
            default="",
            description="整理/生成用的模型任务名（对应 model.toml 的 [model_tasks.xxx]）；留空 = 跟随主回复模型(actor)",
        )
        dream_pre_wake_hours: float = Field(
            default=1.0,
            ge=0.0,
            le=4.0,
            description="临醒前多久由定时任务生成当晚的梦",
        )
        dream_check_minutes: int = Field(
            default=15,
            ge=5,
            le=60,
            description="定时任务检查间隔（分钟）；落在临醒窗口才真正生成",
        )
        journal_max_days: int = Field(
            default=3,
            ge=1,
            le=14,
            description="小本本保留天数（环形）",
        )
        top_k: int = Field(
            default=5,
            ge=1,
            le=10,
            description="每晚送模型整理的显著条目上限",
        )
        turn_buffer_max: int = Field(
            default=40,
            ge=5,
            le=100,
            description="recent_turns 原料缓冲上限",
        )
        consolidate_timeout_seconds: float = Field(
            default=15.0,
            ge=1.0,
            le=60.0,
            description="整理/生成调用超时（秒），超时跳过今晚",
        )
        inject_journal: bool = Field(
            default=True,
            description="是否把生成的梦/「心里记着的事」渗进心境 prompt",
        )

    @config_section("topic")
    class TopicSection(SectionBase):
        """二期：话题相似度（接口预留，默认关闭）。"""

        enabled: bool = Field(
            default=True,
            description="是否启用「TopicSection」模块",
        )
        window: int = Field(
            default=3,
            ge=1,
            le=10,
            description="话题历史保留的最近轮数",
        )

    @config_section("chronic_states")
    class ChronicStatesSection(SectionBase):
        """二期：慢性状态检测（接口预留，默认关闭）。"""

        enabled: bool = Field(
            default=True,
            description="是否启用「ChronicStatesSection」模块",
        )

    @config_section("fallback_reply")
    class FallbackReplySection(SectionBase):
        """二期：动态错误回复 fallback（接口预留，默认关闭）。"""

        enabled: bool = Field(
            default=True,
            description="是否启用「FallbackReplySection」模块",
        )

    @config_section("score")
    class ScoreSection(SectionBase):
        """LLM 打分：由模型判断每轮消息的情绪刺激（v11 对齐）。"""

        enabled: bool = Field(
            default=True,
            description="是否用 LLM 给用户消息打情绪分（关闭则回退词典打分）",
        )
        model_task: str = Field(
            default="",
            description="打分专用模型任务名（对应 model.toml 的 [model_tasks.xxx]）；留空 = 跟随对话模型",
        )
        timeout_seconds: float = Field(
            default=8.0,
            description="打分调用超时（秒），超时自动回退词典打分",
        )
        max_input_chars: int = Field(
            default=600,
            description="送入打分的用户消息最大字符数",
        )

    core: CoreSection = Field(default_factory=CoreSection)
    idle_drift: IdleDriftSection = Field(default_factory=IdleDriftSection)
    topic: TopicSection = Field(default_factory=TopicSection)
    chronic_states: ChronicStatesSection = Field(default_factory=ChronicStatesSection)
    sleep: SleepSection = Field(default_factory=SleepSection)
    fallback_reply: FallbackReplySection = Field(default_factory=FallbackReplySection)
    score: ScoreSection = Field(default_factory=ScoreSection)
