# 赛博内分泌（Hormone System）KFC 插件开发规格

> 交给 AtomCode 实现。目标：把 `hormone_reference.py`（赛博内分泌系统）改造成 KFC 插件，
> 装进 MoFox 框架，为角色提供"情绪惯性"。

---

## 0. 一句话目标

给角色加一套**可衰减、可累积、可被哄动的情绪状态机**，让她的情绪有惯性：
不会因为一句好话立刻 180 度翻转，也不会永远卡在冰点。

## 1. 运行环境（硬事实，别猜）

- 框架：Neo-MoFox **1.3.0-alpha.0**（`stream_api` 2.0），Python 3.11
- 插件目录：`/media/zhongsheng/1140BBD7EC433B291/家/jxdxj-new/plugins/`
- 参考插件（必须读它学接入方式）：`plugins/kfc_memory_bridge/`
- KFC 插件包：`plugins/kokoro_flow_chatter-2.6.8.mfp`

### 接入点（唯一可用事件）

KFC 只暴露一个插件事件：**`on_prompt_build`**。
参数结构：

```python
async def execute(self, event_name: str, params: dict[str, Any]) -> tuple[EventDecision, dict[str, Any]]:
    if params.get("name") != "kfc_user_prompt":
        return EventDecision.SUCCESS, params
    values = params.get("values")          # dict 或 object
    # 读用户消息
    content = values.get("content")        # str 或 list[{"type":"text","text":...}]
    # 注入（追加，不要覆盖已有内容）
    existing = values.get("extra", "") or ""
    values["extra"] = (existing + "\n\n" + injection) if existing else injection
    return EventDecision.SUCCESS, params
```

**关键约束**：
- 只在 `params["name"] == "kfc_user_prompt"` 时处理
- `values["extra"]` 必须**追加**，不能覆盖（其他插件也在用，如 kfc_memory_bridge）
- 用户消息文本可能含 KFC last-mile 指令段（`\n\n---\n` 之后），打分前必须剥离

## 2. 第一版范围（必须实现）

从参考脚本完整移植以下 6 块，**参数可调但算法保持一致**：

| 模块 | 说明 |
|---|---|
| 1. 六激素状态机 | dopamine / cortisol / oxytocin / serotonin / endorphin / empathy，基线 50，各自半衰期 |
| 2. 半衰期衰减 | `BASELINE + (v - BASELINE) * 2^(-h/half_life)`，每次 prompt 构建时按空闲时长结算 |
| 3. 情感打分器 | 中文词典法（正/负词、否定词、"傻瓜"类亲昵豁免），输出 [-1, 1] |
| 4. 情绪记忆窗口 | 最近 8 条情绪分，位置权重 + 时间衰减（半衰期 1h），算 smoothed_aff |
| 5. 疲劳度 | 负面互动累积、正面消减，上限 100；影响激素响应幅度 |
| 6. 心境 prompt 生成 | 把数值翻译成自然语言心境，**禁止出现任何数字/激素名** |

## 3. 二期范围（第一版预留接口，不实现）

以下模块在参考脚本里，第一版**不做**，但架构要能一插即用（留好方法签名 + 配置开关）：

- 空闲自发漂移 `_idle_spontaneous_drift`
- 话题相似度 `_extract_topic_tokens` / `_topic_similarity`
- 慢性状态检测 `_detect_chronic_states`（意义缺失/长期低落/慢性心累/共情倦怠/深度依恋）
- 动态错误回复 fallback

## 4. 文件结构（必须）

```
plugins/hormone_system/
├── manifest.json          # name=hormone_system, version=0.1.0
├── plugin.py              # BasePlugin + 事件处理器
├── config.py              # 配置模型（可调参数）
├── hormone.py             # 核心状态机（从参考脚本移植）
├── scorer.py              # 情感打分器
├── prompt.py              # 心境 prompt 生成
└── state_store.py         # 状态持久化
```

### manifest.json 要点

```json
{
  "name": "hormone_system",
  "display_name": "赛博内分泌",
  "version": "0.1.0",
  "description": "为 KFC 角色提供情绪惯性与疲劳度",
  "min_core_version": "1.3.0-alpha.0",
  "api_version": { "stream_api": "2.0.0" },
  "dependencies": { "plugins": ["kokoro_flow_chatter"], "components": [] },
  "include": [
    { "component_type": "event_handler", "component_name": "hormone_prompt_handler", "enabled": true },
    { "component_type": "config", "component_name": "config", "enabled": true }
  ],
  "entry_point": "plugin.py",
  "python_dependencies": []
}
```

## 5. 状态持久化

- 路径：`<bot>/data/plugins/hormone_system/<stream_id>.json`（按会话隔离）
- 结构参照参考脚本 `save_state()`：hormones / last_interaction_time / emotion_memory / fatigue / version
- **原子写**：写 `.tmp` 再 `os.replace`
- 每次 `on_prompt_build` 处理完立即保存
- 状态损坏时**重置为基线**，不要抛异常（不能因为状态文件坏了让她不回复）

## 6. 默认参数（第一版保守值）

```python
BASELINE = 50.0
HALF_LIVES = {
    "dopamine": 12.0, "cortisol": 6.0, "oxytocin": 72.0,
    "serotonin": 48.0, "endorphin": 4.0, "empathy": 24.0,
}
FATIGUE_HALF_LIFE = 12.0
MEMORY_HALF_LIFE_HOURS = 1.0
MAX_SINGLE_EMOTIONAL_DELTA = 25.0
ATTACHMENT_THRESHOLD = 80.0
MAX_HISTORY = 40
```

**第一版关闭二期功能**（配置里 `idle_drift.enabled = false` 等）。

## 7. Prompt 生成铁律（最重要）

1. **绝不出现数字**：不能写"皮质醇 75"，只能写"心里烦躁、有压力、耐心差"
2. **绝不出现激素名**：不能写"多巴胺旺盛"，只能写"心情很好、表达欲强"
3. **允许情绪惯性**：明确告诉模型"如果仍处于低落中，哪怕对方说好话，也需要时间缓冲，不要立刻 180 度转变"
4. **说短**：整段控制在 300 字以内，避免长段说教
5. **不动物化**：不要"♪"以外的可爱特效，不要"小动物"语气
6. 注入位置：`values["extra"]` 末尾，加分隔标题 `【此刻的身心状态】`

## 8. 验收标准

- [ ] 插件能在 `jxdxj-new` 里被加载，日志出现 `✅ 插件加载成功: hormone_system`
- [ ] 发一条消息后，`data/plugins/hormone_system/<stream>.json` 生成，激素值有变化
- [ ] 连续发 5 条负面消息，cortisol 上升、dopamine 下降、fatigue 上升
- [ ] 连续发 5 条正面消息（抱抱/辛苦了），endorphin 上升、fatigue 下降
- [ ] 时隔 2 小时再发，激素向基线回落
- [ ] 她的回复里**不出现任何激素名或数字**
- [ ] 插件异常时不影响 KFC 正常回复（try/except 全包）

## 9. 参考实现

完整参考脚本：同目录 `hormone_reference.py`（794 行，CLI 版）。
**算法按它来，接入方式按 `kfc_memory_bridge` 来。**

## 10. 不要做的事

- 不要改 KFC 的包（`kokoro_flow_chatter-2.6.8.mfp`）
- 不要动 `config/model.toml`
- 不要引入新的第三方依赖（纯标准库）
- 不要写测试文件到 bot 目录，测试放 `/tmp`
- 不要打印大段日志（每轮最多 1 行 INFO：六个激素值 + 疲劳）
