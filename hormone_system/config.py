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
            default=4,
            description="状态文件版本号（v4 起梦料带 who=user/self）",
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
            default=0.5,
            ge=0.0,
            le=6.0,
            description="睡前多久开始进入「犯困收尾」的语气；默认半小时（定太长她会提前道晚安）",
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
            description="整理/生成用的模型任务名（对应 model.toml 的 [model_tasks.xxx]）；留空 = 从主回复模型(actor)起；首选没成会自动退到 sub_actor/utils_small/utils 备用任务",
        )
        dream_pre_wake_hours: float = Field(
            default=1.0,
            ge=0.0,
            le=4.0,
            description="（已弃用，保留兼容）旧版临醒前生成窗口；现改为熟睡中段生成",
        )
        dream_mid_start_ratio: float = Field(
            default=0.3,
            ge=0.0,
            le=0.9,
            description="梦在睡眠窗的这个比例之后开始生成（0.3=30% 处）",
        )
        dream_mid_end_ratio: float = Field(
            default=0.7,
            ge=0.1,
            le=1.0,
            description="梦生成窗口的结束比例（0.7=70% 处）",
        )
        dream_check_minutes: int = Field(
            default=15,
            ge=5,
            le=60,
            description="定时任务检查间隔（分钟）；落在熟睡窗口才生成梦",
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
            description="recent_turns 原料缓冲上限（旧字段；实际上限取它和 dream_material_max 里的大者）",
        )
        dream_material_max: int = Field(
            default=120,
            ge=20,
            le=400,
            description="梦料条数上限：跨天一起留，梦从这里面抽",
        )
        dream_material_days: float = Field(
            default=2.0,
            ge=1.0,
            le=7.0,
            description="梦料只留最近几天（更早的丢掉）",
        )
        dream_context_lines: int = Field(
            default=1,
            ge=0,
            le=2,
            description="选中一条素材时前后各带几句上下文（0=只留那一句）",
        )
        dream_random_picks: int = Field(
            default=1,
            ge=0,
            le=3,
            description="除按显著性挑的之外再随机抓几条，让梦不是每次都同一批",
        )
        dream_conflict_quota: int = Field(
            default=2,
            ge=0,
            le=5,
            description="最近 24h 内的强负面必须带几条进梦（0=不强制）",
        )
        dream_conflict_threshold: float = Field(
            default=-0.45,
            ge=-1.0,
            le=0.0,
            description="情绪分低于此算「冲突/难受」，用于强制配额和基调下限",
        )
        dream_resolve_threshold: float = Field(
            default=0.6,
            ge=0.0,
            le=1.0,
            description="冲突之后出现不低于此分的正面互动才算「消解」，才允许纯甜梦",
        )
        dream_sweet_needs_resolve: bool = Field(
            default=True,
            description="有未消解的冲突时压成「甜虐」而不是纯甜（关=只看当下心境）",
        )
        turn_clip_chars: int = Field(
            default=120,
            ge=20,
            le=400,
            description="单条入梦原料保留的正文长度（已剥掉转发信封；120≈一到两句完整的话）",
        )
        consolidate_timeout_seconds: float = Field(
            default=25.0,
            ge=1.0,
            le=60.0,
            description="单个模型任务一次的等待上限（秒），超了就换下一个备用任务",
        )
        consolidate_total_seconds: float = Field(
            default=60.0,
            ge=5.0,
            le=180.0,
            description="整理/生成调用的总时间预算（秒，跨备选模型任务共享）；到点就跳过这一轮",
        )
        inject_journal: bool = Field(
            default=True,
            description="是否把生成的梦/「心里记着的事」渗进心境 prompt",
        )
        sleep_onset_gap_minutes: int = Field(
            default=30,
            ge=0,
            le=180,
            description="睡眠窗内来消息，需距上次互动安静≥此分钟数才判「被吵醒」；0=旧行为（窗内即算吵醒）",
        )
        target_sleep_hours: float = Field(
            default=6.0,
            ge=1.0,
            le=14.0,
            description="推荐睡眠时长 / 缺额基准（债按它算）",
        )
        sleep_debt_enabled: bool = Field(
            default=True,
            description="是否启用睡眠债（关=不累计、不影响基线）",
        )
        sleep_debt_cap_hours: float = Field(
            default=12.0,
            ge=1.0,
            le=48.0,
            description="睡眠债上限，防无限累积",
        )
        debt_decay_per_night: float = Field(
            default=0.8,
            ge=0.0,
            le=4.0,
            description="每夜身体自然消退的睡眠债（小时）",
        )
        oversleep_margin_hours: float = Field(
            default=2.5,
            ge=0.0,
            le=6.0,
            description="睡超过 target+此值判「睡太多→睡懵」",
        )
        dynamic_schedule: bool = Field(
            default=True,
            description="是否让她每天自定作息（关=用固定 bedtime/wake_time）",
        )
        timezone: str = Field(
            default="Asia/Shanghai",
            description="睡眠判定所用时区（空/非法回退服务器本地）",
        )
        night_bucket_start: float = Field(
            default=18.0,
            ge=0.0,
            le=24.0,
            description="夜桶起点（测量睡眠的宽带上界），默认 18:00",
        )
        night_bucket_end: float = Field(
            default=12.0,
            ge=0.0,
            le=24.0,
            description="夜桶终点（次日），默认 12:00",
        )
        plan_at: float = Field(
            default=21.5,
            ge=0.0,
            le=24.0,
            description="每晚几点之后可以开始定今晚作息",
        )
        plan_check_minutes: int = Field(
            default=10,
            ge=5,
            le=60,
            description="作息/补觉检查轮询间隔（分钟）",
        )
        plan_sane_min_hours: float = Field(
            default=1.0,
            ge=0.1,
            le=6.0,
            description="计划时长防呆下限（不是政策）",
        )
        plan_sane_max_hours: float = Field(
            default=12.0,
            ge=6.0,
            le=24.0,
            description="计划时长防呆上限（不是政策）",
        )
        earliest_bedtime: float = Field(
            default=20.0,
            ge=0.0,
            le=24.0,
            description="就寝防呆早限（20:00），非政策",
        )
        latest_bedtime: float = Field(
            default=30.0,
            ge=12.0,
            le=36.0,
            description="就寝防呆晚限（>24 表示次日，30=次日06:00），非政策",
        )
        nap_enabled: bool = Field(
            default=True,
            description="是否允许她白天主动补觉（nap）",
        )
        nap_check_minutes: int = Field(
            default=20,
            ge=5,
            le=120,
            description="补觉检查轮询间隔（分钟）",
        )
        nap_quiet_minutes: int = Field(
            default=40,
            ge=10,
            le=240,
            description="安静多久后才考虑补觉",
        )
        nap_min_hours: float = Field(
            default=0.5,
            ge=0.1,
            le=4.0,
            description="最短补觉",
        )
        nap_max_hours: float = Field(
            default=2.0,
            ge=0.5,
            le=6.0,
            description="最长补觉（超过会拖晚夜睡）",
        )
        nap_repay_factor: float = Field(
            default=0.5,
            ge=0.0,
            le=1.0,
            description="补觉回补睡眠债的折扣（低于夜睡）",
        )
        nap_per_day: int = Field(
            default=1,
            ge=0,
            le=3,
            description="每天最多补几次",
        )
        nap_fatigue_threshold: float = Field(
            default=60.0,
            ge=0.0,
            le=100.0,
            description="疲劳超过此才考虑补觉",
        )
        nap_debt_threshold: float = Field(
            default=2.0,
            ge=0.0,
            le=24.0,
            description="睡眠债超过此才考虑补觉",
        )
        nap_recheck_minutes: int = Field(
            default=60,
            ge=10,
            le=360,
            description="问过模型要不要补觉后的冷却（分钟），不论答应与否；防每轮重复调用",
        )
        state_log_minutes: float = Field(
            default=30.0,
            ge=0.0,
            le=1440.0,
            description="后台[State]状态日志的心跳间隔（分钟）；状态有变化时立即打，0=只打变化",
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
