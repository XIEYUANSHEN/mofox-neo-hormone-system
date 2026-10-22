"""hormone_system 第一版冒烟测试（临时脚本，不进 bot 目录）。

验证：
1. 配置模型可用框架 Field 正常实例化，二期开关默认关闭
2. 负面消息 → cortisol 升 / dopamine 降 / fatigue 升
3. 正面消息 → endorphin 升 / fatigue 降
4. 时隔 2 小时 → 激素向基线回落
5. 心境 prompt：无数字、无激素名、有惯性提示、300 字预算
6. 状态存储：save/load 往返、损坏文件重置、路径安全
7. 打分器：正面/负面/否定翻转/亲昵豁免
8. 事件处理器端到端（含追加不覆盖、异常兜底）
"""

import asyncio
import os
import re
import sys
import time

BOT = "/media/zhongsheng/1140BBD7EC433B291/家/jxdxj-new"
sys.path.insert(0, os.path.join(BOT, "plugins"))
sys.path.insert(0, BOT)

from hormone_system.config import HormoneSystemConfig  # noqa: E402
from hormone_system.hormone import HORMONE_KEYS, HormoneSystem  # noqa: E402
from hormone_system.prompt import build_mood_prompt  # noqa: E402
from hormone_system.scorer import SentimentScorer  # noqa: E402
from hormone_system.state_store import HormoneStateStore  # noqa: E402

STATE_DIR = "/home/zhongsheng/hormone_plugin_dev/.smoke_state"
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


# 1. 配置
cfg = HormoneSystemConfig()
check("配置实例化", cfg.core.enabled is True and cfg.core.baseline == 50.0)
check("二期开关默认关闭",
      cfg.idle_drift.enabled is False and cfg.topic.enabled is False
      and cfg.chronic_states.enabled is False and cfg.fallback_reply.enabled is False)
check("半衰期默认值", cfg.core.half_lives["oxytocin"] == 72.0 and cfg.core.half_lives["endorphin"] == 4.0)
check("记忆窗口=8", cfg.core.memory_window == 8)

# 7. 打分器
sc = SentimentScorer()
check("打分-正面", sc.score("你真棒，爱你！") > 0.5, f"={sc.score('你真棒，爱你！')}")
check("打分-负面", sc.score("你真烦，讨厌你") < -0.5, f"={sc.score('你真烦，讨厌你')}")
check("打分-否定翻转", sc.score("你不烦") > 0, f"={sc.score('你不烦')}")
check("打分-亲昵豁免", sc.score("你个小傻瓜") >= 0, f"={sc.score('你个小傻瓜')}")

# 2. 负面消息
sys1 = HormoneSystem(cfg)
sys1.last_interaction_time = time.time()
before_cort, before_da, before_fat = (
    sys1.hormones["cortisol"], sys1.hormones["dopamine"], sys1.fatigue)
for msg in ["你烦死了", "滚远点", "讨厌你，别烦我", "真没意思", "不想跟你聊了"]:
    sys1.update_hormone_by_text(msg)
    sys1.last_interaction_time = time.time()
check("负面×5 cortisol 上升", sys1.hormones["cortisol"] > before_cort,
      f"{before_cort:.1f}→{sys1.hormones['cortisol']:.1f}")
check("负面×5 dopamine 下降", sys1.hormones["dopamine"] < before_da,
      f"{before_da:.1f}→{sys1.hormones['dopamine']:.1f}")
check("负面×5 fatigue 上升", sys1.fatigue > before_fat, f"{before_fat:.1f}→{sys1.fatigue:.1f}")

# 3. 正面消息
sys2 = HormoneSystem(cfg)
sys2.last_interaction_time = time.time()
b_end, b_ox = sys2.hormones["endorphin"], sys2.hormones["oxytocin"]
sys2.update_hormone_by_text("抱抱你，辛苦了，我在")
sys2.update_hormone_by_text("我爱你么么哒，你真的很棒")
sys2.update_hormone_by_text("辛苦了辛苦了，抱抱")
check("正面 endorphin 上升", sys2.hormones["endorphin"] > b_end,
      f"{b_end:.1f}→{sys2.hormones['endorphin']:.1f}")
check("正面 oxytocin 上升", sys2.hormones["oxytocin"] > b_ox,
      f"{b_ox:.1f}→{sys2.hormones['oxytocin']:.1f}")
sys3 = HormoneSystem(cfg)
sys3.fatigue = 50.0
sys3.last_interaction_time = time.time()
sys3.update_hormone_by_text("抱抱，辛苦了，我在，别难过")
check("正面 fatigue 下降", sys3.fatigue < 50.0, f"50.0→{sys3.fatigue:.1f}")

# 4. 时隔 2 小时回落（单独验证半衰期衰减，避免本轮消息的正常加成干扰）
sys4 = HormoneSystem(cfg)
sys4.hormones = {k: 80.0 for k in HORMONE_KEYS}
sys4.fatigue = 60.0
sys4.last_interaction_time = time.time() - 2 * 3600
idle = sys4._apply_decay()
# 所有激素都应向基线移动（80→50 方向）；半衰期最短的 endorphin(4h) 应回落最多
check("2h 衰减：全部向基线移动",
      abs(idle - 2.0) < 0.05
      and all(v < 80.0 for v in sys4.hormones.values())
      and sys4.hormones["endorphin"] < sys4.hormones["oxytocin"],
      str({k: round(v, 1) for k, v in sys4.hormones.items()}))
# 60 * 2^(-2/12) ≈ 53.45
check("2h 衰减：fatigue 按半衰期回落", abs(sys4.fatigue - 53.45) < 1.0, f"60→{sys4.fatigue:.1f}")

# 5. prompt 铁律
sys5 = HormoneSystem(cfg)
sys5.hormones = {"dopamine": 25, "cortisol": 85, "oxytocin": 50,
                 "serotonin": 45, "endorphin": 30, "empathy": 55}
sys5.fatigue = 75.0
sys5._last_idle_hours = 4.0
prompt = build_mood_prompt(sys5, cfg)
HORMONE_NAMES = ("多巴胺", "皮质醇", "催产素", "血清素", "内啡肽")
check("prompt 无数字", not re.search(r"\d", prompt))
check("prompt 无激素名", not any(w in prompt for w in HORMONE_NAMES))
check("prompt 有惯性提示", "惯性" in prompt)
check("prompt 有注入标题", prompt.startswith("【此刻的身心状态】"))
check("prompt 长度", len(prompt) <= 300, f"len={len(prompt)}")
print("---- prompt 示例 ----")
print(prompt)
print("---------------------")

sys6 = HormoneSystem(cfg)
sys6.hormones = {"dopamine": 90, "cortisol": 90, "oxytocin": 20,
                 "serotonin": 20, "endorphin": 90, "empathy": 20}
sys6.fatigue = 90.0
sys6._last_idle_hours = 5.0
p6 = build_mood_prompt(sys6, cfg)
check("极端情况仍 ≤300 字（可选段被丢弃）", len(p6) <= 300, f"len={len(p6)}")

# 6. 状态存储
store = HormoneStateStore(STATE_DIR)
st = sys1.to_state(cfg.core.state_version)
store.save("stream-test-1", st)
loaded = store.load("stream-test-1")
check("状态 save/load 往返", loaded is not None
      and abs(loaded["hormones"]["cortisol"] - sys1.hormones["cortisol"]) < 1e-6
      and loaded["fatigue"] == sys1.fatigue)
sys_restored = HormoneSystem(cfg)
sys_restored.apply_state(loaded)
check("apply_state 还原激素",
      abs(sys_restored.hormones["dopamine"] - sys1.hormones["dopamine"]) < 1e-6)
with open(os.path.join(STATE_DIR, "stream-bad.json"), "w", encoding="utf-8") as f:
    f.write("{broken json!!")
check("损坏状态返回 None（重置基线）", store.load("stream-bad") is None)
check("stream_id 路径安全",
      store.state_path("../../etc/passwd").startswith(STATE_DIR),
      store.state_path("../../etc/passwd"))

# 8/9. 插件模块 + 事件处理器端到端
import hormone_system.plugin as hp  # noqa: E402

check("plugin 模块导入 & 类定义",
      hp.HormonePromptHandler.handler_name == "hormone_prompt_handler"
      and hp.HormonePromptHandler.init_subscribe == ["on_prompt_build"]
      and issubclass(hp.HormoneSystemPlugin, hp.BasePlugin))


class _FakePlugin:
    def __init__(self, config):
        self.config = config


# e2e 用独立配置，状态目录指到测试目录（避免写进 bot 数据目录）
cfg_e2e = HormoneSystemConfig()
cfg_e2e.core.state_dir = STATE_DIR
handler = hp.HormonePromptHandler(_FakePlugin(cfg_e2e))
values = {"content": "[新消息]\n你个笨蛋，滚\n\n---\n行为指令...",
          "extra": "【既有插件内容】", "stream_id": "smoke-e2e"}
params = {"name": "kfc_user_prompt", "values": values}
result, _ = asyncio.run(handler.execute("on_prompt_build", params))
check("e2e 返回 SUCCESS", result is hp.EventDecision.SUCCESS)
check("e2e extra 追加不覆盖",
      values["extra"].startswith("【既有插件内容】") and "【此刻的身心状态】" in values["extra"])
state_file = os.path.join(STATE_DIR, "smoke-e2e.json")
check("e2e 状态文件生成", os.path.exists(state_file))
extra_part = values["extra"].split("【此刻的身心状态】", 1)[1]
check("e2e prompt 无数字无激素名",
      not re.search(r"\d", extra_part) and not any(w in extra_part for w in HORMONE_NAMES))
p2 = {"name": "other_prompt", "values": {"content": "x"}}
r2, p2out = asyncio.run(handler.execute("on_prompt_build", p2))
check("非 KFC 事件直通", r2 is hp.EventDecision.SUCCESS and p2out is p2)
p3 = {"name": "kfc_user_prompt"}
r3, _ = asyncio.run(handler.execute("on_prompt_build", p3))
check("values 缺失不抛异常", r3 is hp.EventDecision.SUCCESS)

print()
if failures:
    print(f"❌ {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("✅ 全部冒烟测试通过")
