import sys, os, time as _time, asyncio, tempfile, json
from datetime import datetime
from zoneinfo import ZoneInfo

T = os.environ.get("MOFOX_HOME") or os.getcwd()   # 在实例根目录下跑，或用 MOFOX_HOME 指定
sys.path.insert(0, T)
sys.path.insert(0, os.path.join(T, "plugins"))

from hormone_system.config import HormoneSystemConfig
from hormone_system.hormone import HormoneSystem
from hormone_system.state_store import HormoneStateStore
from hormone_system import plugin as hp
from hormone_system.prompt import build_mood_prompt

# 可控时钟
REAL_TIME = _time.time
class Clock:
    now = 0.0
_time.time = lambda: Clock.now
tz = ZoneInfo("Asia/Shanghai")

def ts(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=tz).timestamp()

fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(info)) if info else ""))
    if not cond:
        fails.append(name)

def new_cfg(state_dir):
    c = HormoneSystemConfig()
    c.core.enabled = True
    c.core.state_dir = state_dir
    c.score.enabled = False
    c.sleep.enabled = True
    c.sleep.consolidate = True
    c.sleep.dynamic_schedule = True
    c.sleep.nap_enabled = True
    c.sleep.consolidate_model_task = "hormone_sleep"
    c.sleep.plan_at = 21.5
    c.sleep.target_sleep_hours = 6.0
    c.sleep.consolidate_timeout_seconds = 10.0
    return c

def inst(cfg):
    i = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
    i.config = cfg
    i._schedule_ids = []
    i._register_task_id = None
    i._state_sig = {}
    i._state_ts = {}
    return i

def seed(store, cfg, sid, **kw):
    s = HormoneSystem(cfg)
    s.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 85.0,
                  "serotonin": 70.0, "endorphin": 75.0, "empathy": 60.0}
    s.emotion_memory = [(0.8, Clock.now)]
    s.recent_turns = [{"text": "他说最喜欢你", "score": 0.8, "ts": Clock.now}]
    for k, v in kw.items():
        setattr(s, k, v)
    store.save(sid, s.to_state(cfg.core.state_version))
    return s

# ---- mock 模型 ----
async def fake_plan(config, system, report=None):
    return {"bedtime": 23.5, "duration_h": 6.5, "note": "t", "source": "model"}

async def fake_dream(config, items, tone, report=None):
    return {"dream": "梦见和你走在夜路上", "recall": "fuzzy", "worth": False}

async def fake_nap(config, system, report=None):
    return {"nap": True, "dur_h": 1.0, "note": "t"}

o_plan, o_dream, o_nap = hp._generate_sleep_plan, hp._generate_dream, hp._generate_nap
hp._generate_sleep_plan = fake_plan
hp._generate_dream = fake_dream
hp._generate_nap = fake_nap

# ================= A：完整一夜（规划→睡前聊→熟睡做梦→醒来结算） =================
dirA = tempfile.mkdtemp(prefix="simA_")
cfgA = new_cfg(dirA)
Clock.now = ts(23, 21, 30)
storeA = HormoneStateStore(dirA)
seed(storeA, cfgA, "A", last_interaction_time=ts(23, 20, 0), last_sleep_settle=ts(23, 6, 30))

asyncio.run(inst(cfgA)._consolidate_job())
st = storeA.load("A") or {}
pl = st.get("sleep_plan")
check("A1 21:30 自动定了今晚作息", isinstance(pl, dict) and abs(pl.get("bedtime", 0) - 23.5) < 1e-6,
      f"plan={pl}")

# 23:40 还在聊（5 分钟前刚说过话）-> 不该判吵醒
Clock.now = ts(23, 23, 40)
sA = HormoneSystem(cfgA); sA.apply_state(storeA.load("A"))
sA.last_interaction_time = ts(23, 23, 35)
sA.update_hormone_by_text("还在聊呢")
check("A2 睡前一直聊不判吵醒", sA.sleep_interrupted_at == 0.0 and sA._sleep_stage != "night_wake",
      f"stage={sA._sleep_stage} interrupted={sA.sleep_interrupted_at}")
storeA.save("A", sA.to_state(cfgA.core.state_version))

# 睡前一直聊到 02:30（密集发言=醒着），02:30 之后才真睡 -> 这一夜约睡 4h
sA.sleep_awake_spans = [[ts(23, 23, 40), ts(24, 2, 25)]]
storeA.save("A", sA.to_state(cfgA.core.state_version))

# 03:00 熟睡中段 -> 生成梦草稿
Clock.now = ts(24, 3, 0)
asyncio.run(inst(cfgA)._consolidate_job())
st = storeA.load("A") or {}
pd = st.get("pending_dream")
check("A3 熟睡中生成梦草稿", isinstance(pd, dict) and pd.get("text") == "梦见和你走在夜路上", f"pd={pd}")

# 06:50 醒来（假设 03:00 后睡着，实际睡 3.5h）-> 结算 + 转正
Clock.now = ts(24, 6, 50)
sA2 = HormoneSystem(cfgA); sA2.apply_state(storeA.load("A"))
sA2.last_interaction_time = ts(24, 3, 0)
sA2.update_hormone_by_text("早安")
check("A4 醒来结算：短睡+判定 fuzzy+进小本本",
      sA2.last_sleep_kind == "short" and sA2.sleep_debt > 0
      and isinstance(sA2.gen_dream, dict) and sA2.gen_dream.get("recall") == "fuzzy"
      and sA2.gen_dream.get("worth") is True and len(sA2.sleep_journal) == 1
      and sA2.pending_dream is None,
      f"kind={sA2.last_sleep_kind} debt={sA2.sleep_debt:.1f} gd={sA2.gen_dream}")
storeA.save("A", sA2.to_state(cfgA.core.state_version))
sA3 = HormoneSystem(cfgA); sA3.apply_state(storeA.load("A"))
check("A5 结算结果已落盘", isinstance(sA3.gen_dream, dict) and len(sA3.sleep_journal) == 1)
sA3.wake_up_turns = 0; sA3.wake_up_reason = ""
# A4 的 quality 可能因抖动落到 bad（那走"潜意识阴影"，本就不该有美梦行），
# 这里明确成"睡短了但没睡坏"，专测美梦行能不能挤进 prompt
sA3.sleep_quality = "short"; sA3.sleep_thoughts = ""
p = build_mood_prompt(sA3, cfgA)
check("A6 梦能进 prompt", "梦里还留着甜" in p, p[:50])

# ================= B：通宵（整夜在聊）-> 不做梦、记债 =================
dirB = tempfile.mkdtemp(prefix="simB_")
cfgB = new_cfg(dirB)
Clock.now = ts(23, 21, 30)
storeB = HormoneStateStore(dirB)
seed(storeB, cfgB, "B", last_interaction_time=ts(24, 6, 40))
sB = HormoneSystem(cfgB); sB.apply_state(storeB.load("B"))
sB.apply_sleep_plan(23.5, 7.0, "model", "", now=ts(23, 21, 30))
sB.set_pending_dream("通宵不该有梦", "sweet")
storeB.save("B", sB.to_state(cfgB.core.state_version))
Clock.now = ts(24, 6, 50)
sB2 = HormoneSystem(cfgB); sB2.apply_state(storeB.load("B"))
sB2.update_hormone_by_text("天都亮了")
check("B1 通宵：记债、丢弃草稿", sB2.last_sleep_kind == "allnighter" and sB2.sleep_debt > 4.0
      and sB2.pending_dream is None and sB2.gen_dream is None,
      f"kind={sB2.last_sleep_kind} debt={sB2.sleep_debt:.1f}")

# ================= C：白天补觉（疲劳高+安静够） =================
dirC = tempfile.mkdtemp(prefix="simC_")
cfgC = new_cfg(dirC)
Clock.now = ts(24, 14, 0)
storeC = HormoneStateStore(dirC)
seed(storeC, cfgC, "C", fatigue=80.0, sleep_debt=3.0, last_interaction_time=ts(24, 13, 0))
asyncio.run(inst(cfgC)._consolidate_job())
st = storeC.load("C") or {}
nap = st.get("nap")
check("C1 白天累+安静→自动补觉", isinstance(nap, dict) and abs(nap.get("dur_h", 0) - 1.0) < 1e-6, f"nap={nap}")

Clock.now = ts(24, 14, 20)
sC = HormoneSystem(cfgC); sC.apply_state(storeC.load("C"))
debt_before = sC.sleep_debt
sC.last_interaction_time = ts(24, 13, 0)
sC.update_hormone_by_text("在吗")
check("C2 补觉被吵醒(轻)+回补一点债", sC._sleep_stage == "nap_wake" and sC.nap is None
      and sC.sleep_debt < debt_before,
      f"stage={sC._sleep_stage} debt {debt_before:.2f}->{sC.sleep_debt:.2f}")

# ================= D：日志人话 =================
summ = sA3.sleep_state_summary()
check("D1 状态摘要人话", "今晚" in summ and "睡眠债" in summ and "睡眠质量" in summ, summ[:60])

# 恢复真实时钟 / 模型
_time.time = REAL_TIME
hp._generate_sleep_plan, hp._generate_dream, hp._generate_nap = o_plan, o_dream, o_nap

# ================= E：真实模型一次性连通（非致命） =================
try:
    Clock.now = ts(24, 21, 30)
    cE = new_cfg(tempfile.mkdtemp(prefix="simE_"))
    sE = HormoneSystem(cE); sE.fatigue = 55.0; sE.sleep_debt = 1.0
    real = asyncio.run(hp._generate_sleep_plan(cE, sE))
    print("INFO 真实模型计划:", real)
    check("E1 真实模型返回可用计划", isinstance(real, dict) and "bedtime" in real, real)
except Exception as e:
    print("INFO 真实模型不可用(环境问题，不判失败):", repr(e))

print("\n==== RESULT ====")
print("FAILED:", fails if fails else "NONE")
sys.exit(1 if fails else 0)
