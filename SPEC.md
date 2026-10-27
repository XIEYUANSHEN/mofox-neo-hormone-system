# 赛博内分泌（Hormone System）插件规格 · 现行 v0.4.1

> 本文是**现行实现规格**：给要改这个插件的人（或 AI）看的事实与约束。
> 历史沿革与最初那版「交给 AtomCode 实现」的 v0.1 范围收在 §12，其余章节一律以 0.4.1 的代码为准。

## 0. 目标

给角色一套**有惯性、有身体后果**的身心状态：情绪不因一句好话 180 度翻转，也不会永远卡在冰点；睡觉是**真的会影响明天言行**的事件——她几点睡是自己定的，睡得好不好会影响激素，熟睡里会做梦，醒来才决定记不记得，熬夜会欠债，累了可以自己决定补一觉。

对外只有一条硬要求：**所有拟真都必须藏在自然语言里**，她的回复与注入文本不能出现数字、激素名或"系统提示"腔。

## 1. 运行环境（硬事实，别猜）

- 框架：Neo-MoFox **1.3.0-alpha.0**（`pyproject.toml` 里 `name = "neo-mofox"`），`stream_api 2.0.0` / `event_api 1.0.0` / `llm_api 1.0.0` / `log_api 1.0.0`
- Python：3.11（实例自带 `.venv`）
- 插件目录：`/media/zhongsheng/1140BBD7EC433B291/家/<实例>/plugins/hormone_system/`
  ——**在 NTFS 挂载盘上，不在 `~` 下，`find ~` 会漏**
- 角色编排：KFC（`plugins/kokoro_flow_chatter-2.6.8.mfp`），`manifest.json` 声明依赖它
- 参考插件（学接入方式）：`plugins/kfc_memory_bridge/`

## 2. 接入点

KFC 只暴露一个插件事件：**`on_prompt_build`**，本插件的 `HormonePromptHandler`（`weight=5`, `intercept_message=False`）订阅它：

```python
async def execute(self, event_name, params):
    if params.get("name") != "kfc_user_prompt":
        return EventDecision.SUCCESS, params
    values = params.get("values")            # dict 或 object
    # 读消息 → 结算激素 → 追加注入
    existing = values.get("extra", "") or ""
    values["extra"] = (existing + "\n\n" + injection) if existing else injection
    return EventDecision.SUCCESS, params
```

另有两条只读命令：`DreamReviewCommand`（`/查看昨晚梦境`，审计梦与小本本）和 `SleepStatusCommand`（`/睡眠状态`，看当晚作息/相位/债/补觉/梦/梦料条数，只报数量不贴原文）。

**关键约束**

- 只在 `params["name"] == "kfc_user_prompt"` 时处理；`values["extra"]` 必须**追加**，不得覆盖（别的插件也在写）。
- 用户消息可能带 KFC last-mile 指令段（`\n\n---\n` 之后），打分与入梦原料前必须剥离。
- 整条链路 `try/except` 包住：插件异常绝不能影响她回复。
- 处理完立即原子保存状态（同一次交互在 stream 锁内）。

## 3. 文件结构与职责

```
plugins/hormone_system/
├── manifest.json      # name=hormone_system, version=0.4.1, entry=plugin.py, AGPL-3.0
├── __init__.py
├── plugin.py          # BasePlugin + 事件处理器 + 命令 + 后台睡眠任务 + 模型调用封装
├── config.py          # pydantic 配置模型（framework 据此生成 config.toml 并注释每项）
├── hormone.py         # HormoneSystem 核心状态机（激素/疲劳/睡眠/梦/债/补觉）
├── scorer.py          # 中文词典情感打分器（LLM 不可用时回退）
├── prompt.py          # 心境注入文本生成（含睡眠相位语气与梦境行）
└── state_store.py     # 按 stream 的 JSON 持久化（原子写、损坏宽容）
```

`plugin.py` 里值得先认识的符号：`HormoneSystemPlugin._consolidate_job`（周期任务三段：定作息 / 补觉 / 生成梦）、`_dream_window_bounds`、`_consolidate_tasks` + `_call_consolidate_llm`（模型任务候选链与双层预算）、`_generate_dream` / `_generate_sleep_plan` / `_generate_nap`、`_fallback_plan`、`_note_once`（一次一句的日志节流）、`_should_log_state`（`[State]` 心跳）、`_lock_for`。

`hormone.py` 里的睡眠判定链：`_night_bounds` / `_night_key`（夜桶，默认 18:00 → 次日 12:00）→ `_resolve_plan` → `_resolve_window`（有 plan 用 plan，否则 config）→ `_in_window` / `_quiet_slept` → `_apply_sleep` → `_score_sleep` → `_settle_last_sleep` → `_judge_pending_dream` / `_promote_pending_dream`；另有 `current_sleep_phase`（纯时间相位，供无消息回合）、`should_plan_sleep` / `apply_sleep_plan`、`should_nap` / `start_nap` / `_finalize_nap`、`set_pending_dream`、`get_generation_input`。

## 4. 状态持久化

- 路径：`<bot>/data/plugins/hormone_system/<stream_id>.json`（`stream_id` 过文件名安全化，空 → `default`）
- **原子写**：写 `.tmp` 再 `os.replace`；读失败/结构异常 → 返回 `None`，调用方重置基线，**不抛异常**
- 每次 `on_prompt_build` 处理完立即存；后台任务在锁内读→算→存
- `core.state_version = 4`（v4 起梦料带 `who=user/self`）。现行字段（生产实况）：

  `version, hormones, last_interaction_time, emotion_memory, recent_gaps, fatigue, topic_history, wake_up_turns, wake_up_reason, last_sleep_time, last_sleep_settle, last_sleep_kind, sleep_score, sleep_quality, sleep_thoughts, sleep_interrupted_at, sleep_debt, sleep_short_streak, sleep_awake_spans, sleep_plan, nap, nap_date, nap_check_ts, recent_turns, pending_dream, gen_dream, sleep_journal`

- `recent_turns` 每条形如 `{text, score, ts, who, clipped}`；旧状态没有 `who/clipped` 时默认 `user / false`。
- 多实例 / 多 stream 相互隔离；跨机迁移要注意 `recent_turns` 里的原料文本是本地语料，不含凭据。

## 5. 睡眠状态机

1. **夜归属**：一"夜"= 夜桶 `[night_bucket_start, night_bucket_end)`（默认 18:00→次日 12:00），`night_key` 取桶起始日；`sleep_plan` 只在同 `night_key` 内有效，过期自然回退到 config 固定作息。
2. **定作息**：`dynamic_schedule` 开、当前钟点 ≥ `plan_at`、且今夜还没有 plan → 后台问模型 `{bedtime, duration_h, note}`；`apply_sleep_plan` 做防呆夹取（就寝 `[earliest_bedtime, latest_bedtime]` = 20:00–次日06:00，时长 1–12h，**傍晚/凌晨分别处理**，且不能定在过去）；拿不到可用模型 → `_fallback_plan`（越累越早睡、睡越久），日志会说明原因。
3. **睡着判定**：睡眠窗内出现用户消息，只有「距上次互动安静 ≥ `sleep_onset_gap_minutes`」才置 `sleep_interrupted_at`（被吵醒）；一直聊 = 她选择陪着你，不算吵醒。阈值 0 = 旧的"窗内即吵醒"。
4. **睡眠测量**：`_quiet_slept` 在「本夜就寝(前 0.5h 宽限)→计划起床点」内累计安静时长，上限 14h。
   窗内她醒着的时刻由 `_record_awake_in_night` 记进 `sleep_awake_spans`（**区间**，相邻 <10 分钟自动并成一段，长聊不会把记录撑爆）：**两句话间隔 <10 分钟算同一段清醒，超过 10 分钟的静默才算睡着**——
   所以半夜被摸一下头、亲一口不会再让整夜作废（实测：旧口径把 6.5h 的一夜算成 1.1h）。
   `sleep_awake_spans` 为空（旧状态 / 中途重启）时退回旧口径「上次互动 → 起床点」，不凭空多算。
5. **结算**：第一次互动时结算；触发条件含通宵（`pre_time ≥ bed_ts`）；首启（无历史状态）与离线 >36h 不结算。`_score_sleep` 产出 `sleep_score` 与 `good/short/bad`，按质量回写激素与起床气（`wake_up_turns`/`wake_up_reason`）。
6. **睡眠债**：`deficit = target - slept` 累加，`repay = min(债, max(0, slept-target)*0.7)`，每夜再自然消退 `debt_decay_per_night`；分类 `allnighter / short / normal / catchup / oversleep`（睡超 `target + oversleep_margin_hours` → 睡懵：降多巴胺、加疲劳）。债的存在会抬皮质醇底线、压血清素上限。**设计取向：债不必还清。**
7. **补觉（nap）**：白天安静 ≥ `nap_quiet_minutes` 且（疲劳 > `nap_fatigue_threshold` 或 债 > `nap_debt_threshold`）且当天未补过且 `nap_per_day` 未满 → 问模型要不要睡、睡多久（0.5–2h，`nap_repay_factor` 打折回补）；问过就写 `nap_check_ts`，`nap_recheck_minutes` 内不再问（**被拒也不每轮重问**）。nap 期间消息 = 轻度吵醒（`nap_wake`）。
8. **梦**：见 §7。`dream_pre_wake_hours` 已弃用（保留字段兼容旧 config.toml），现行窗口是熟睡中段比例。

## 6. 相位与注入（prompt.py）

- `current_sleep_phase` 纯时间判定，返回 `wind_down / sleeping / groggy / ""`；`dynamic_schedule` 开而今晚没 plan 时返回空串（别硬套语气）。
- `_SLEEP_TONE` 三段语气；**wind_down 特例**：今晚有 `sleep_plan` 时改用 `_sleep_tone_line()`，把**她自己定的就寝钟点**写进同一句话（只说困意不给钟点，她会提前道晚安）。
- 梦境行优先级：起床缓冲期压住一切梦境 → 噩梦走「潜意识的阴影」（`quality=bad` + 高皮质醇或低血清素 + 距上次睡眠 < `shadow_window_hours`）→ 否则 `worth` 且 `recall!=none` 且未过期的 `gen_dream` 按基调补一条（`fuzzy` 会明确标注"记得很模糊，别补细节"）。
- `sleeping` 相位不叠加"独处等待/失落"，免得与"半梦半醒"自相矛盾。
- 预算：`MAX_PROMPT_CHARS = 420`，超预算的可选行按优先级丢弃（睡眠段优先）。实现上 `build_mood_prompt` **先算出睡眠段并预留其长度**再往激素行里塞，所以激素行再多也挤不掉「梦里还留着甜」；实测满负荷块 360 字。

## 7. 梦的生成与判定（两刀分开）

- **素材从哪来（v0.4.2 重做）**：
  - 来源是**双方**：他说的话在 `on_prompt_build` 里进料（先剥 KFC 转发信封，压成「昵称：正文」；消息 id 不要求纯数字，`proactive_xxx` 也认）；
    **KFC 主动发言/自发思考这类内部提示不算他的话**（`<spontaneous_thought>` / `<inner_thought>` / `<system_reminder>` / `<concept>` 直接忽略：不评分、不进梦料、不刷新互动时间；历史里混进来的读盘时洗掉）。她自己发出去的话由 `HormoneSentHandler` 订阅 `on_message_sent` 进料（只取 `processed_plain_text`，`[图片/[表情/[语音/…` 这类占位与非文本跳过；没有状态文件的流不新建）。每条存 `{text, score, ts, who, clipped}`，按 `turn_clip_chars`（120）截，`clipped` 记录是否被截。
  - 容量：上限取 `max(turn_buffer_max, dream_material_max=120)` 条，超过 `dream_material_days=2` 天的直接丢（`_prune_turns`）。
  - 她自己的话不打分，显著性固定 0.30；**被截断的长回复减半到 0.15**（半句解释不适合当梦料）。
  - 抽样（`get_generation_input`）：① 最近 24h 里 `score ≤ dream_conflict_threshold` 的强负面必须带 `dream_conflict_quota=2` 条；② 按时间分层「今晚(≤6h) / 今天早些(6–24h) / 更早(24h–2d)」各取最显著的一条；③ 余下按显著性补齐 `top_k`；④ 再随机抓 `dream_random_picks=1` 条换掉最弱的**非强制**项；⑤ 选中项带前后各 `dream_context_lines=1` 句上下文，拼成「他：…／我：…」片段（相邻的会合并，但单个片段最多 `2*ctx+2` 句，避免把一晚并成一大段），片段之间**打乱顺序**，整块不超过 620 字。
  - **基调下限**：`R ≤ DREAM_R_NIGHTMARE` 仍然 nightmare（走阴影，不产内容）；否则若最近 24h 存在**未消解**的强负面（其后没有 `score ≥ dream_resolve_threshold=0.6` 的正面），基调封顶 `ambivalent`，**不给纯甜**（`dream_sweet_needs_resolve=true` 可关）。冲突窗口按 24h 算，比素材两天窗口窄，这是有意的。
  - 生成提示词（`_DREAM_PROMPT`）明确：梦是碎的、可换场景/缝在一起；**不要照抄原句、不要把发生过的事按原样讲一遍、不要总结**；素材里有冲突就留着它，不要只挑好的一面。输出 40–70 字。
  - 模型偶尔把 JSON 截断：`_coerce_dream(parsed, raw)` 会从原文里把 `"dream":"…"` 抠出来，不让一夜白丢。
- **第一刀·熟睡中段生成内容**：`_dream_window_bounds` 求出本夜 `(bed_ts, wake_ts)`，`frac = (now-bed)/(wake-bed)` 落在 `[dream_mid_start_ratio, dream_mid_end_ratio)`（默认 0.3–0.7）才动手；先过三道闸——安静满 onset 阈值（还在聊不做）、本窗口未生成过（`gen_dream.ts` 或 `pending_dream.gen_ts` 早于窗起点才算没做过）、有原料（`recent_turns` Top-`top_k`）。基调由 `_compute_smoothed_aff` + 催产素/皮质醇阈值决定：`nightmare / sweet / ambivalent / neutral`，**噩梦不产内容**，交回阴影路径。
- **第二刀·醒来确定性判定**：`_judge_pending_dream` 按 `slept` 与 `was_interrupted` 定 `recall`，`worth = recall!=none and (recall=="clear" or tone in (sweet, ambivalent))`；`_promote_pending_dream` 只把 `worth` 的写进 `gen_dream` 与小本本，草稿超过 24h 直接丢弃；通宵（`allnighter`）丢弃草稿。
- 生成成功即清空 `recent_turns`（一夜一个梦）；**模型没成时不清原料**，下一轮继续试。

## 8. 后台调度与并发

- 注册：`on_plugin_loaded` → `_register_schedule_when_ready` 轮询等 scheduler 就绪（scheduler 在 `run()` 才 start），用 `get_unified_scheduler().create_schedule(..., TriggerType.TIME, {"interval_seconds": N}, is_recurring=True, task_name="hormone_sleep_dream", force_overwrite=True)`。
- 间隔 `N = min(plan_check_minutes, nap_check_minutes, dream_check_minutes) × 60`，下限 60s（默认 10 分钟一轮，**不是只有做梦在看**）。
- 每个状态文件一把 `asyncio.Lock`（`_lock_for(state_path)`），事件处理器与后台任务共用同一 key → 同一 stream 串行；**模型调用一律在锁外**，两段短临界区（读判定 / 重载后二次去重再写），等模型这段时间里别的回合可能已生成过 → 临界区 2 再去重一次。
- 单流异常只警告并跳过，不影响其它流；每条 stream 用最新 `time.time()`（避免多流 + 模型耗时导致 `now` 过期）。
- 卸载时 `remove_schedule` + 取消注册协程。

## 9. 模型调用（三层防护）

1. **候选链**：`_consolidate_tasks()` = `consolidate_model_task`（配置值，留空跳过）→ `actor` → `sub_actor` → `utils_small` → `utils`，去重。前面的任务「不存在 / 接口报错 / 超时 / 空回复」才换下一个；换用成功打一行 `[Model] … 换用备用模型任务 …`。
2. **双层预算**：单任务 `consolidate_timeout_seconds`（25s）；整链共享 `consolidate_total_seconds`（60s），剩余 <1s 就停。预算用 `time.monotonic()` 计（不受测试里 patch `time.time` 影响）。
3. **失败可见**：`_generate_*` 收一个可选 `report: dict`，失败原因写 `report["error"]`；调用方 `_note_once(key, token=(窗起点, 原因), 消息)` 保证**同一晚同一原因只说一句 INFO**。`_reason()` 负责把空消息异常（如 `asyncio.TimeoutError`）也压成有字的短语。
4. 任何情况下不抛异常出去。打分链路（`_score_with_llm`）**不做**候选链（它在回复路径上，超时 8s 直接回退词典，避免拖慢回复）。
5. headless 跑真模型前必须 `init_model_config("config/model.toml")`，否则 `get_model_set_by_task` 全失败、一律走兜底，极易误判成代码坏了。

## 10. 日志（人话 + 节流）

```
[Plan] stream=23715084 今晚 23:40 睡 → 06:10 醒（6.5h，她自己定的）｜睡眠债 0.0h｜…
[Plan] stream=… 今晚作息没问成模型，改按身体情况自己定了：actor: ValueError 任务 'actor' 无可用渠道
[Dream] stream=… 今晚不做梦：到该做梦的点了，但她还在聊（才安静 5 分钟，要满 30 分钟才算睡着）
[Model] hormone_dream 换用备用模型任务 utils_small（前面 actor/sub_actor 没成）
[DreamPend] stream=… 熟睡中做了个甜的梦，醒来再判定记不记得/值不值得记
[Sleep] 睡了 6.4h｜质量良好（92）｜被吵醒=否｜…｜疲劳 12→0
[State] stream=… 一行睡眠摘要（变化即打，否则 state_log_minutes 心跳）
```

- `[Dream]` 的节流键 = `(本夜睡眠窗起点, 稳定原因码)`（`chatting / no-material / nightmare / done`），**文案里可以带分钟数，键里绝不能带**——否则每轮的"安静 N 分钟"都会被当成新原因，一夜刷四行（v0.4.1 实盘踩到过）。分钟数向下取整，避免"安静 30 分钟，要满 30 分钟"这种自相矛盾。
- 禁止把激素名或裸数字塞进她的台词文本；日志可以带钟点与百分比。
- 日志 helper 必须**懒初始化**（`getattr(self, "_xxx", None)`），裸实例（测试里 `__new__` 造出来的）不能因为缺属性把整轮任务拖崩。

## 11. 验收标准

- [ ] 实例日志出现 `✅ 插件加载成功: hormone_system v0.4.1` 与 `睡眠后台任务已注册`
- [ ] `plan_at` 之后 `[Plan]` 落 `sleep_plan`，且 `night_key` 与当前夜一致
- [ ] 睡前一直聊 → 醒来 `[Sleep]` 的 `被吵醒=否`，睡眠分不被扣
- [ ] 熟睡中段（默认 01:30–04:15 视作息）出现 `[DreamPend]` 或明确的「今晚不做梦：…」
- [ ] 醒来第一次互动出现 `[Sleep]` 结算，`worth` 的梦进 `gen_dream` / `sleep_journal`
- [ ] 把 `actor` 弄成不可用，`get_model_set_by_task` 链仍能用备用任务做出梦（真模型实测过）
- [ ] `/查看昨晚梦境` 能读出她记住的梦；她的回复里没有激素名/数字
- [ ] 状态文件损坏 → 重置基线，不崩、不影响回复
- [ ] 单测 **187 条**全绿（仓库根目录 `test_hormone_v030.py`，在实例根目录跑）+ 整夜 mock 端到端（`test_sim_v04_e2e.py`）
- [ ] 入梦原料形如 `昵称：正文`，没有 `[消息id:`/`[QQ:`/`[回复<` 残渣；一条 60 字的话能完整进原料
- [ ] 她自己说的话也进梦料（`who=self`），片段里能看到「我：…」
- [ ] 最近 24h 吵过、之后没被哄好时，梦的基调不会是纯甜（至少甜虐）

**测试写法约束（踩过）**：断言不得绑真实钟点（23:00 后跑会被"不能定在过去"防呆顺推）；相位/注入类断言要**冻结 `time.time`** 到指定时刻；mock `_generate_*` 时签名要含 `report=None`；**别让随机抖动决定断言分支**——`_score_sleep` 会对激素加抖动，e2e 的 A4 偶尔落成 `quality=bad`（那走潜意识的阴影，本就不该有美梦行），所以 A6 先显式把 quality 设成 `short` 再断言「梦里还留着甜」。

## 12. 配置默认值速查（v0.4.1 实测读回）

`[core]` `enabled=true` `state_dir=data/plugins/hormone_system` `baseline=50` `half_lives={多巴胺12, 皮质醇6, 催产素72, 血清素48, 内啡肽4, 共情24}` `fatigue_half_life=12` `memory_half_life_hours=1` `max_single_emotional_delta=25` `attachment_threshold=80` `memory_window=8` `max_history=40` `state_version=4`

`[sleep]` `enabled=true` `bedtime=23.5` `wake_time=7.0` **`wind_down_hours=0.5`** `groggy_hours=1.0` `shadow_window_hours=24` `consolidate=true` `consolidate_model_task=""` `dream_mid_start_ratio=0.3` `dream_mid_end_ratio=0.7` `dream_check_minutes=15` `journal_max_days=3` `top_k=5` `turn_buffer_max=40` `dream_material_max=120` `dream_material_days=2.0` `dream_context_lines=1` `dream_random_picks=1` `dream_conflict_quota=2` `dream_conflict_threshold=-0.45` `dream_resolve_threshold=0.6` `dream_sweet_needs_resolve=true` **`turn_clip_chars=120`** `consolidate_timeout_seconds=25.0` `consolidate_total_seconds=60.0` `inject_journal=true` `sleep_onset_gap_minutes=30` `target_sleep_hours=6.0` `sleep_debt_enabled=true` `sleep_debt_cap_hours=12` `debt_decay_per_night=0.8` `oversleep_margin_hours=2.5` `dynamic_schedule=true` `timezone="Asia/Shanghai"` `night_bucket_start=18.0` `night_bucket_end=12.0` `plan_at=21.5` `plan_check_minutes=10` `plan_sane_min_hours=1` `plan_sane_max_hours=12` `earliest_bedtime=20` `latest_bedtime=30` `nap_enabled=true` `nap_check_minutes=20` `nap_quiet_minutes=40` `nap_min_hours=0.5` `nap_max_hours=2` `nap_repay_factor=0.5` `nap_per_day=1` `nap_fatigue_threshold=60` `nap_debt_threshold=2` `nap_recheck_minutes=60` `state_log_minutes=30` `dream_pre_wake_hours=1.0`（已弃用，只为兼容旧 config.toml）

`[score]` `enabled=true` `model_task=""`（跟随对话模型）`timeout_seconds=8.0` `max_input_chars=600`；`[idle_drift]/[topic]/[chronic_states]/[fallback_reply]` 默认开（`fallback_reply` 只留接口，未接生成链路）。

**陷阱**：`config.toml` 一旦生成，**升级插件里的默认值不会改它**（例：`wind_down_hours` 由 1.5 改 0.5 需同时改各实例的 `config/plugins/hormone_system/config.toml`）。新增字段才会按新默认补写。

## 13. 不要做的事

- 不要改 KFC 的包（`kokoro_flow_chatter-*.mfp`），不要动 `config/model.toml`
- 不要引入第三方依赖（纯标准库 + 框架 API）
- 不要把测试文件写进 bot 目录，测试放 `/tmp`
- 不要为了后台整理去拖慢回复：模型调用在锁外、有预算、失败可跳过
- 不要宽泛 `pkill`（会连带杀掉别的角色进程）；**bot 重启一律由维护者手动做**
- 不要写"给人看不懂"的日志：每行要说清是谁、发生了什么、为什么

## 14. 沿革

- **v0.1（原始规格）**：只做六激素状态机 + 词典/LLM 打分 + 情绪记忆 + 疲劳 + 心境注入，二期功能（空闲漂移/话题/慢性/fallback）留接口；当时的验收与范围已完成，二期后来逐个接上了。参考实现见同目录 `hormone_reference.py`（CLI 版，`simulate_sleep` 是那条线的起点）。
- **v0.2.0**：睡眠 ↔ 激素双向耦合（睡眠质量 / 梦境碎片 / 起床缓冲 / 潜意识阴影 / 相位语气），按墙钟自动结算。
- **v0.3.0**：睡眠记忆整理与「小本本」——临醒前生成梦、醒来判定 recall/worth、`pending_dream`、`journal_max_days`。
- **v0.4.0**：她自定今晚作息（planning + 防呆 + 兜底公式）、睡着判定（安静阈值）、睡眠债与睡懵/补觉回补、白天主动补觉、梦移到熟睡中段生成 + 醒来确定性判定、日志人话化与节流。多轮自审共修 12 个真问题（就寝前安静被算作睡眠、调度间隔忽略 plan/nap、离线 >36h 仍结算、草稿无过期、转正梦缺 `ts`、19:00 被夹成 06:00、补觉被拒每 10 分钟重问、通宵不结算、跨午夜去重错位、醒着也生成梦、起床后时间算睡眠、日志 helper 拖垮任务）。
- **v0.4.2**：重做梦的素材机制——双方发言入料（新增 `on_message_sent` 处理器）、跨两天 120 条、多时间层分层抽样＋随机、片段化并打乱、冲突强制配额与基调下限（未消解的吵架不给纯甜梦）；容忍被截断的模型输出；睡眠时长改按整夜累计安静（`sleep_awake_spans` 区间，<10 分钟间隔算同一段），半夜被吵醒不再作废整夜；内部提示不再当他说的话；新增 `/睡眠状态` 只读概览；`state_version=4`。
- **v0.4.1**：整理类调用加模型任务候选链与双层时间预算（主回复模型欠费时梦还能做出来）；梦被挡/失败用人话各说一句并节流（键改稳定原因码）；入梦原料剥壳留名（`昵称：正文`，昵称截 12 字；只剩壳的消息不占原料）＋长度改 `turn_clip_chars=120`；注入预算给睡眠段预留位置；收尾期注入她自己定的就寝钟点；`wind_down_hours` 默认 1.5 → 0.5；删「临醒窗口」死代码。
- **v0.4.1 实盘复盘（2026-10-24 第一夜）**：01:37 主模型 `actor` 失败、`sub_actor` 兜底把两个会话的梦都做出来；醒来结算 7.0h/质量良好、`recall=clear worth=True` 进小本本；樱爱那台 3.3h 被吵醒的夜仍按 `fuzzy+甜` 判成值得记；同夜暴露两个真问题——节流键里带分钟数导致一夜四行、原料 80 字被信封吃掉 63 字只剩 17 字（已修）。

设计稿（仓库外，维护者机器上）：`睡眠小本本_设计草案`、`睡眠v0.4_自定作息与睡着判定_设计草案`、`睡眠v0.4_代码级实施方案`。
