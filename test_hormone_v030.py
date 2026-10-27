import sys, os, time, random, re, asyncio, tempfile

T = os.environ.get("MOFOX_HOME") or os.getcwd()   # 在实例根目录下跑，或用 MOFOX_HOME 指定
sys.path.insert(0, T)
sys.path.insert(0, os.path.join(T, "plugins"))

from hormone_system.config import HormoneSystemConfig
from hormone_system.hormone import HormoneSystem
from hormone_system.state_store import HormoneStateStore
from hormone_system.prompt import build_mood_prompt, _sleep_lines, _clean_dream
from hormone_system import plugin as hp

DAY = 24.0 * 3600.0
fails = []
def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(info)) if info else ""))
    if not cond:
        fails.append(name)

def cfg_consolidate(on=True, task="hormone_sleep", **over):
    c = HormoneSystemConfig()
    c.core.enabled = True
    c.score.enabled = False
    c.sleep.enabled = True
    c.sleep.consolidate = on
    c.sleep.consolidate_model_task = task
    c.sleep.dynamic_schedule = False   # 旧测试按固定作息；P3 测试单独开
    c.sleep.bedtime = 23.5
    c.sleep.wake_time = 7.0
    c.sleep.shadow_window_hours = 24.0
    for k, v in over.items():
        setattr(c.sleep, k, v)
    return c

# ---------- 1. 配置字段 ----------
c = HormoneSystemConfig()
check("1.1 新字段存在", all(hasattr(c.sleep, k) for k in
      ["consolidate","consolidate_model_task","dream_pre_wake_hours","dream_check_minutes",
       "journal_max_days","top_k","turn_buffer_max","consolidate_timeout_seconds","inject_journal"]))
check("1.2 consolidate 默认 true", c.sleep.consolidate is True)
check("1.3 journal 默认 3 / check 默认15", c.sleep.journal_max_days == 3 and c.sleep.dream_check_minutes == 15)
check("1.4 state_version 默认 4（梦料带 who）", c.core.state_version == 4)

# ---------- 2. recent_turns 仅 consolidate 时缓冲 ----------
sys1 = HormoneSystem(cfg_consolidate(True))
sys1.update_hormone_by_text("今天很开心呀")
check("2.1 consolidate=true 缓冲 recent_turns", len(sys1.recent_turns) == 1 and "text" in sys1.recent_turns[0])
sys0 = HormoneSystem(cfg_consolidate(False))
sys0.update_hormone_by_text("随便一句话")
check("2.2 consolidate=false 不缓冲原文", sys0.recent_turns == [])

# ---------- 3. get_generation_input 基调选择 ----------
random.seed(1)
g = HormoneSystem(cfg_consolidate(True))
g.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 85.0, "serotonin": 70.0, "endorphin": 75.0, "empathy": 60.0}
g.recent_turns = [{"text": "他说最喜欢你", "score": 0.8, "ts": time.time()}]
items, tone = g.get_generation_input()
check("3.1 偏正+高依恋 → sweet/ambivalent", tone in ("sweet", "ambivalent"), f"tone={tone} items={len(items)}")
gn = HormoneSystem(cfg_consolidate(True))
gn.hormones = {"dopamine": 20.0, "cortisol": 80.0, "oxytocin": 50.0, "serotonin": 25.0, "endorphin": 30.0, "empathy": 50.0}
gn.recent_turns = [{"text": "被你凶了", "score": -0.8, "ts": time.time()}]
gn.emotion_memory = [(-0.8, time.time())]
_, tn = gn.get_generation_input()
check("3.2 偏负 → nightmare(交回旧路径)", tn == "nightmare", f"tone={tn}")
gz = HormoneSystem(cfg_consolidate(True))
gz.hormones = {"dopamine": 50.0, "cortisol": 50.0, "oxytocin": 55.0, "serotonin": 50.0, "endorphin": 50.0, "empathy": 50.0}
gz.recent_turns = [{"text": "吃了饭", "score": 0.0, "ts": time.time()}]
_, tz = gz.get_generation_input()
check("3.3 平淡 → neutral", tz == "neutral", f"tone={tz}")
check("3.4 consolidate=false → 无原料", HormoneSystem(cfg_consolidate(False)).get_generation_input() == ([], None))

# ---------- 4. apply_generated_dream ----------
random.seed(5)
a = HormoneSystem(cfg_consolidate(True))
a.hormones = {"dopamine": 60.0, "cortisol": 40.0, "oxytocin": 80.0, "serotonin": 60.0, "endorphin": 60.0, "empathy": 60.0}
a.recent_turns = [{"text": "想你", "score": 0.6, "ts": time.time()}]
a.apply_generated_dream("梦见和你散步", "sweet", "clear", True)
check("4.1 写 gen_dream", isinstance(a.gen_dream, dict) and a.gen_dream["tone"] == "sweet")
check("4.2 worth=true 进 journal", len(a.sleep_journal) == 1)
check("4.3 清空 recent_turns", a.recent_turns == [])
a.apply_generated_dream("不重要的梦", "neutral", "fuzzy", False)
check("4.4 worth=false 不进 journal", len(a.sleep_journal) == 1)
# journal 上限 3
b = HormoneSystem(cfg_consolidate(True))
for i in range(5):
    b.apply_generated_dream(f"梦{i}", "sweet", "clear", True)
check("4.5 journal 环形≤3", len(b.sleep_journal) == 3)

# ---------- 5. state 往返 ----------
rt = HormoneSystem(cfg_consolidate(True))
rt.apply_generated_dream("一个梦", "sweet", "fuzzy", True)  # 会清空 recent_turns
st = rt.to_state(2)
rt2 = HormoneSystem(cfg_consolidate(True))
rt2.apply_state(st)
check("5.2 gen_dream 往返", rt2.gen_dream and rt2.gen_dream["tone"] == "sweet")
check("5.3 sleep_journal 往返", len(rt2.sleep_journal) == len(rt.sleep_journal))
# recent_turns 往返（不调整理，才不被清空）
rtb = HormoneSystem(cfg_consolidate(True))
rtb.recent_turns = [{"text": "abc", "score": 0.5, "ts": 123.0}]
rtb2 = HormoneSystem(cfg_consolidate(True))
rtb2.apply_state(rtb.to_state(2))
check("5.1 recent_turns 往返", len(rtb2.recent_turns) == 1)
rt3 = HormoneSystem(cfg_consolidate(True))
rt3.apply_state({"recent_turns": "bad", "sleep_journal": [1,2], "gen_dream": [9]})
check("5.4 坏字段容错", rt3.recent_turns == [] and rt3.sleep_journal == [] and rt3.gen_dream is None)

# ---------- 6. prompt 浮现 ----------
NO_H = ["多巴胺","皮质醇","催产素","血清素","内啡肽","共情"]
# 美梦注入
s = HormoneSystem(cfg_consolidate(True))
s.hormones = {"dopamine": 60.0, "cortisol": 30.0, "oxytocin": 80.0, "serotonin": 60.0, "endorphin": 60.0, "empathy": 60.0}
s.gen_dream = {"text": "梦见和他走在很远的夜路上", "tone": "sweet", "recall": "clear", "worth": True, "ts": time.time()}
p = build_mood_prompt(s, cfg_consolidate(True))
check("6.1 美梦浮现", "梦里还留着甜" in p)
# recall=none 不注入
s2 = HormoneSystem(cfg_consolidate(True))
s2.gen_dream = {"text": "梦见什么", "tone": "sweet", "recall": "none", "worth": True, "ts": time.time()}
check("6.2 recall=none 不注入", "梦里还留着甜" not in "".join(_sleep_lines(s2, cfg_consolidate(True), "")))
# worth=false 不注入
s3 = HormoneSystem(cfg_consolidate(True))
s3.gen_dream = {"text": "碎梦", "tone": "neutral", "recall": "clear", "worth": False, "ts": time.time()}
check("6.3 worth=false 不注入", "做了个梦" not in "".join(_sleep_lines(s3, cfg_consolidate(True), "")))
# 含数字 → 判废
check("6.4 _clean_dream 去数字", _clean_dream("梦见3点见面") == "")
check("6.5 _clean_dream 去激素名", _clean_dream("多巴胺很高") == "")
s4 = HormoneSystem(cfg_consolidate(True))
s4.gen_dream = {"text": "他抱着我，很暖", "tone": "sweet", "recall": "fuzzy", "worth": True, "ts": time.time()}
ln = _sleep_lines(s4, cfg_consolidate(True), "")
txt = "\n".join(ln)
check("6.6 fuzzy 标注模糊", "模糊" in txt)
check("6.7 梦行无数字", not re.search(r"[0-9]", txt) or not any("梦" in x for x in ln if re.search(r"[0-9]", x)))
check("6.8 梦行无激素名", not any(h in txt for h in NO_H))
# 噩梦保持 v0.2.0：quality bad + 高皮质醇 + turns0 → 潜意识阴影
s5 = HormoneSystem(cfg_consolidate(True))
s5.sleep_quality = "bad"; s5.sleep_thoughts = "梦到追不上"; s5.last_sleep_time = time.time(); s5.wake_up_turns = 0
s5.hormones = {"dopamine": 40.0, "cortisol": 80.0, "oxytocin": 50.0, "serotonin": 40.0, "endorphin": 40.0, "empathy": 50.0}
txt5 = "\n".join(_sleep_lines(s5, cfg_consolidate(True), ""))
check("6.9 噩梦仍走潜意识阴影", "潜意识的阴影" in txt5)
# 起床期压制梦境
s6 = HormoneSystem(cfg_consolidate(True))
s6.wake_up_turns = 2; s6.wake_up_reason = "起床气文案"
s6.gen_dream = {"text": "甜甜的梦", "tone": "sweet", "recall": "clear", "worth": True, "ts": time.time()}
txt6 = "\n".join(_sleep_lines(s6, cfg_consolidate(True), ""))
check("6.10 起床态压制美梦", "起床状态" in txt6 and "梦里还留着甜" not in txt6)
s7 = HormoneSystem(cfg_consolidate(True))
s7.hormones = {"dopamine": 60.0, "cortisol": 30.0, "oxytocin": 80.0, "serotonin": 60.0, "endorphin": 60.0, "empathy": 60.0}
s7.gen_dream = {"text": "很久以前的梦", "tone": "sweet", "recall": "clear", "worth": True, "ts": time.time() - 30 * 3600}
check("6.11 超时效旧梦不浮现", "梦里还留着甜" not in "".join(_sleep_lines(s7, cfg_consolidate(True), "")))

# ---------- 7. 熟睡梦窗口 ----------
w = HormoneSystem(cfg_consolidate(True))
wake = w._day_anchor(time.time(), 7.0)
cfgW = cfg_consolidate(True)

def _in_dream_window(sysobj, now, config):
    """测试用判定：与梦段同样先看睡眠窗、再看熟睡中段比例。"""
    b = hp._dream_window_bounds(sysobj, now, config)
    if b is None:
        return False
    frac = (now - b[0]) / (b[1] - b[0])
    return bool(config.sleep.dream_mid_start_ratio <= frac < config.sleep.dream_mid_end_ratio)

check("7.1 03:00 在梦窗口", _in_dream_window(w, wake - 4 * 3600, cfgW) is True)
check("7.2 06:30 过窗口", _in_dream_window(w, wake - 0.5 * 3600, cfgW) is False)
check("7.3 01:00 还没到", _in_dream_window(w, wake - 6 * 3600, cfgW) is False)
check("7.4 04:30 在窗口", _in_dream_window(w, wake - 2.5 * 3600, cfgW) is True)
tLate = w._day_anchor(time.time(), 0.0) + 23 * 3600 + 30 * 60
bd = hp._dream_window_bounds(w, tLate, cfgW)
check("7.5 23:30 bounds 用今夜", bd is not None and abs(bd[0] - tLate) < 60, f"bd={bd}")
tEarly = w._day_anchor(time.time(), 0.0) + 26 * 3600
bd2 = hp._dream_window_bounds(w, tEarly, cfgW)
check("7.6 02:00 bounds 昨夜23:30",
      bd2 is not None and abs(bd2[0] - (w._day_anchor(time.time(), 0.0) + 23.5 * 3600)) < 60,
      f"bd2={bd2}")

# ---------- 8. 并发锁 ----------
l1 = hp._lock_for("streamX"); l2 = hp._lock_for("streamX")
check("8.1 per-stream 锁复用同对象", l1 is l2 and isinstance(l1, asyncio.Lock))
check("8.2 不同 stream 不同锁", hp._lock_for("A") is not hp._lock_for("B"))

# ---------- 9. _generate_dream 模型不可用时安全跳过（不联网） ----------
async def gen_tests():
    import src.app.plugin_system.api.llm_api as llm
    orig = llm.get_model_set_by_task
    def boom(name):
        raise RuntimeError("no model configured")
    llm.get_model_set_by_task = boom
    try:
        # 空 task → 解析成 actor，actor 也不可用 → None（不抛）
        r1 = await hp._generate_dream(cfg_consolidate(True, task=""), [{"text": "x"}], "sweet")
        check("9.1 空task且actor不可用→None(跳过)", r1 is None)
        # 坏 task → 回退 actor，也不可用 → None
        r2 = await hp._generate_dream(cfg_consolidate(True, task="no_such_zzz"), [{"text": "x"}], "sweet")
        check("9.2 坏task回退actor仍不可用→None", r2 is None)
    finally:
        llm.get_model_set_by_task = orig
asyncio.run(gen_tests())

# ---------- 10. 调度 API 可用(create/remove, 不依赖网络) ----------
async def sched_test():
    from src.kernel.scheduler import get_unified_scheduler, TriggerType
    sch = get_unified_scheduler()
    async def dummy():
        return None
    try:
        await sch.start()
    except Exception as e:
        print("  start err:", repr(e))
    try:
        sid = await sch.create_schedule(callback=dummy, trigger_type=TriggerType.TIME,
              trigger_config={"interval_seconds": 3600}, is_recurring=True,
              task_name="hormone_selftest", force_overwrite=True)
        ok = bool(sid)
        await sch.remove_schedule(sid)
        return ok
    except Exception as e:
        print("  sched err:", repr(e))
        return False
    finally:
        try:
            await sch.stop()
        except Exception:
            pass
ok = asyncio.run(sched_test())
check("10.1 create_schedule/remove_schedule 可用", ok)

# ---------- 11. _consolidate_job 端到端（mock 生成，不联网） ----------
async def job_test():
    tmpd = tempfile.mkdtemp(prefix="hormone_jobtest_")
    cfg = cfg_consolidate(True, task="hormone_sleep")
    cfg.core.state_dir = tmpd
    lt = time.localtime()
    hour = lt.tm_hour + lt.tm_min / 60.0
    cfg.sleep.bedtime = (hour - 4.0) % 24.0
    cfg.sleep.wake_time = (hour + 4.0) % 24.0
    store = HormoneStateStore(tmpd)
    seed = HormoneSystem(cfg)
    seed.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 85.0,
                     "serotonin": 70.0, "endorphin": 75.0, "empathy": 60.0}
    seed.emotion_memory = [(0.8, time.time())]
    seed.recent_turns = [{"text": "他说最喜欢你", "score": 0.8, "ts": time.time()}]
    seed.last_interaction_time = time.time() - 3 * 3600
    store.save("jobuser", seed.to_state(cfg.core.state_version))

    seen = {"tone": None}
    async def fake_gen(config, items, tone, report=None):
        seen["tone"] = tone
        return {"dream": "梦见和你走在夜路上", "recall": "clear", "worth": True}
    inst = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
    inst.config = cfg
    orig = hp._generate_dream
    hp._generate_dream = fake_gen
    try:
        await inst._consolidate_job()
    finally:
        hp._generate_dream = orig
    out = store.load("jobuser")
    gd = out.get("pending_dream") if out else None
    check("11.1 基调=sweet/ambivalent", seen["tone"] in ("sweet", "ambivalent"), f"tone={seen['tone']}")
    check("11.2 写回 pending_dream", isinstance(gd, dict) and gd.get("text") == "梦见和你走在夜路上")
    check("11.3 job 后 recent_turns 清空", bool(out) and out.get("recent_turns") == [])

    calls = {"n": 0}
    async def count_gen(config, items, tone, report=None):
        calls["n"] += 1
        return {"dream": "x", "recall": "clear", "worth": True}
    hp._generate_dream = count_gen
    try:
        inst2 = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
        inst2.config = cfg
        await inst2._consolidate_job()
    finally:
        hp._generate_dream = orig
    check("11.4 同窗口去重·不再调模型", calls["n"] == 0, f"calls={calls['n']}")
asyncio.run(job_test())

# ---------- 12. _coerce_dream 纯函数 ----------
check("12.1 None/非dict→None", hp._coerce_dream(None) is None and hp._coerce_dream([1]) is None)
check("12.2 空dream→None", hp._coerce_dream({"dream": ""}) is None)
check("12.3 缺省worth=False", hp._coerce_dream({"dream": "x"})["worth"] is False)
check("12.4 bool True 通过", hp._coerce_dream({"dream": "x", "worth": True})["worth"] is True)
check("12.5 字符串false→False", hp._coerce_dream({"dream": "x", "worth": "false"})["worth"] is False)
check("12.6 '是'/'1'/'true'→True", all(hp._coerce_dream({"dream": "x", "worth": v})["worth"] is True for v in ("是", "1", "true")))
r = hp._coerce_dream({"dream": "x", "recall": "乱填"})
check("12.7 非法recall→fuzzy", r["recall"] == "fuzzy")
check("12.8 长dream截断", len(hp._coerce_dream({"dream": "字" * 300})["dream"]) <= 160)

# ---------- 13. P1 睡着判定（gap 闸） ----------
def mk_win(onset):
    cfg = cfg_consolidate(True)
    lt = time.localtime()
    h = lt.tm_hour + lt.tm_min / 60.0
    cfg.sleep.bedtime = (h - 1.0) % 24.0
    cfg.sleep.wake_time = (h + 1.0) % 24.0
    cfg.sleep.sleep_onset_gap_minutes = onset
    s = HormoneSystem(cfg)
    s.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
    return s, cfg

sA, cA = mk_win(30); sA.last_interaction_time = time.time() - 10 * 60
sA.update_hormone_by_text("还在聊呢")
check("13.1 gap<阈不判吵醒", sA.sleep_interrupted_at == 0.0 and sA.fatigue < 4.0, f"stage={sA._sleep_stage}")

sB, cB = mk_win(30); sB.last_interaction_time = time.time() - 60 * 60
sB.update_hormone_by_text("睡了吗")
check("13.2 gap≥阈判吵醒", sB.sleep_interrupted_at > 0.0 and sB.hormones["cortisol"] > 50.0)

sC, cC = mk_win(0); sC.last_interaction_time = time.time() - 10 * 60
sC.update_hormone_by_text("在吗")
check("13.3 阈=0 退回旧行为(窗内即吵醒)", sC.sleep_interrupted_at > 0.0)

# ---------- 14. P2 睡眠债 ----------
def debt_sys(debt=0.0):
    c = cfg_consolidate(True)
    s = HormoneSystem(c)
    s.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
    s.sleep_debt = debt
    return s, c

pre = {"cortisol": 40.0, "dopamine": 50.0, "serotonin": 50.0}
sD, _ = debt_sys(0.0)
sD._settle_last_sleep(pre, 4.0, True, time.time())
check("14.1 短睡累债", abs(sD.sleep_debt - 1.2) < 1e-6 and sD.last_sleep_kind == "short", f"debt={sD.sleep_debt}")

sA, _ = debt_sys(0.0)
sA._settle_last_sleep(pre, 0.0, False, time.time())
check("14.2 通宵大额欠债", sA.last_sleep_kind == "allnighter" and sA.sleep_debt > 4.0, f"debt={sA.sleep_debt}")

sC, _ = debt_sys(4.0)
sC._settle_last_sleep(pre, 8.0, False, time.time())
check("14.3 补觉偿还", sC.last_sleep_kind == "catchup" and sC.sleep_debt < 4.0, f"debt={sC.sleep_debt}")

sO, _ = debt_sys(0.0)
sO._settle_last_sleep(pre, 9.0, False, time.time())
check("14.4 睡太多→睡懵", sO.last_sleep_kind == "oversleep" and sO.fatigue >= 2.0)

cOff = cfg_consolidate(True); cOff.sleep.sleep_debt_enabled = False
sOff = HormoneSystem(cOff)
sOff.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sOff._settle_last_sleep(pre, 0.0, False, time.time())
check("14.5 关闭债→0", sOff.sleep_debt == 0.0 and sOff.last_sleep_kind == "normal")

sBnd, _ = debt_sys(5.0)
sBnd.hormones["cortisol"] = 30.0
sBnd.hormones["serotonin"] = 90.0
sBnd._apply_debt_bounds()
check("14.6 债抬皮质醇/压血清素", sBnd.hormones["cortisol"] >= 54.0 and sBnd.hormones["serotonin"] <= 97.0)

sCap, _ = debt_sys(11.9)
sCap._settle_last_sleep(pre, 0.0, False, time.time())
check("14.7 债封顶", sCap.sleep_debt <= 12.0 + 1e-6, f"debt={sCap.sleep_debt}")

sRT, cRT = debt_sys(3.3); sRT.last_sleep_kind = "short"
sRT2 = HormoneSystem(cRT); sRT2.apply_state(sRT.to_state(3))
check("14.8 债/类型往返", abs(sRT2.sleep_debt - 3.3) < 1e-6 and sRT2.last_sleep_kind == "short")

# ---------- 15. P3 自定作息 ----------
cP = cfg_consolidate(True); cP.sleep.dynamic_schedule = True
sP = HormoneSystem(cP)
check("15.1 自定作息配置", cP.sleep.dynamic_schedule is True and abs(cP.sleep.plan_at - 21.5) < 1e-6)

base = sP._day_anchor(time.time(), 0.0)
t1 = base + 23 * 3600 + 59 * 60
t2 = base + 24 * 3600 + 60
t3 = base + 26 * 3600
k1, k2, k3 = sP._night_key(t1), sP._night_key(t2), sP._night_key(t3)
check("15.2 夜归属跨午夜一致", k1 == k2 == k3, f"{k1}/{k2}/{k3}")

tPlan = base + 18 * 3600 + 30 * 60
sR = HormoneSystem(cP)
p1 = sR.apply_sleep_plan(23.5, 7.0, "model", "", now=tPlan)
check("15.3 正常计划", abs(p1["bedtime"] - 23.5) < 1e-6 and abs(p1["wake"] - 6.5) < 0.01)

p2 = HormoneSystem(cP).apply_sleep_plan(14.0, 20.0, "model", "", now=tPlan)
bt = p2["bedtime"] if p2["bedtime"] >= 20 else p2["bedtime"] + 24
check("15.4 防呆夹取", p2["dur_h"] <= 12.0 and 20.0 <= bt <= 30.0, f"bed={p2['bedtime']} dur={p2['dur_h']}")
p19 = HormoneSystem(cP).apply_sleep_plan(19.0, 6.0, "model", "", now=tPlan)
check("15.4b 傍晚过早→夹到20:00", abs(p19["bedtime"] - 20.0) < 1e-6, f"bed={p19['bedtime']}")
p2b = HormoneSystem(cP).apply_sleep_plan(2.0, 6.0, "model", "", now=tPlan)
check("15.4c 凌晨2点保留", abs(p2b["bedtime"] - 2.0) < 1e-6, f"bed={p2b['bedtime']}")
p21 = HormoneSystem(cP).apply_sleep_plan(21.0, 6.0, "model", "", now=t1)
bt21 = p21["bedtime"] if p21["bedtime"] >= 20 else p21["bedtime"] + 24
check("15.4d 不能定在过去", bt21 >= 23.9, f"bed={p21['bedtime']}")

b_, w_ = sR._resolve_window(tPlan)
check("15.5 窗口读 plan", abs(b_ - 23.5) < 1e-6 and abs(w_ - 6.5) < 0.01)

sQ = HormoneSystem(cP); tq = sQ._day_anchor(time.time(), 22.5)
check("15.6 未定时 should_plan", sQ.should_plan_sleep(tq) is True)
sQ.apply_sleep_plan(23.5, 6.5, "model", "", now=tq)
check("15.7 定时后不重复", sQ.should_plan_sleep(tq) is False)

cN = cfg_consolidate(True); cN.sleep.dynamic_schedule = True
lt = time.localtime(); h = lt.tm_hour + lt.tm_min / 60.0
cN.sleep.bedtime = (h - 1) % 24; cN.sleep.wake_time = (h + 1) % 24
sN = HormoneSystem(cN)
sN.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sN.last_interaction_time = time.time() - 3600
sN.update_hormone_by_text("还在聊")
check("15.8 无plan不判吵醒", sN.sleep_interrupted_at == 0.0, f"stage={sN._sleep_stage}")

cW = cfg_consolidate(True); cW.sleep.dynamic_schedule = True
sW = HormoneSystem(cW)
sW.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
nowW = time.time(); hW = sW._hours_since_midnight(nowW)
sW.sleep_plan = {"night_key": sW._night_key(nowW), "bedtime": (hW - 1.0) % 24,
                 "dur_h": 2.0, "wake": (hW + 1.0) % 24, "decided_at": nowW,
                 "source": "model", "note": ""}
sW.last_interaction_time = nowW - 3600
sW.update_hormone_by_text("醒了吗")
check("15.9 有plan+安静够→吵醒", sW.sleep_interrupted_at > 0.0)

qs = sP._quiet_slept(base + 23 * 3600, base + 31 * 3600, 23.0, 7.0)
check("15.10 夜桶安静测量", qs > 7.0, f"qs={qs:.1f}")
qs2 = sP._quiet_slept(base + 18 * 3600, base + 31 * 3600, 23.0, 7.0)
check("15.11 就寝前安静不算睡", qs2 <= 8.6, f"qs2={qs2:.1f}")
qs3 = sP._quiet_slept(base + 23 * 3600, base + 32 * 3600, 23.0, 7.0)
check("15.12 起床后时间不计睡眠", qs3 <= 8.2, f"qs3={qs3:.1f}")

# ---------- 16. P3 planning job 端到端（mock 生成，不联网） ----------
async def plan_job_test():
    tmpd = tempfile.mkdtemp(prefix="hormone_plantest_")
    cfg = cfg_consolidate(False, task="hormone_sleep")
    cfg.sleep.dynamic_schedule = True
    cfg.core.state_dir = tmpd
    cfg.sleep.plan_at = 0.0
    store = HormoneStateStore(tmpd)
    seed = HormoneSystem(cfg)
    seed.hormones = {"dopamine": 55.0, "cortisol": 45.0, "oxytocin": 60.0,
                     "serotonin": 55.0, "endorphin": 50.0, "empathy": 55.0}
    store.save("planuser", seed.to_state(3))

    async def fake_plan(config, system, report=None):
        return {"bedtime": 23.0, "duration_h": 6.5, "note": "t", "source": "model"}
    orig = hp._generate_sleep_plan
    hp._generate_sleep_plan = fake_plan
    try:
        inst = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin); inst.config = cfg
        await inst._consolidate_job()
    finally:
        hp._generate_sleep_plan = orig
    out = store.load("planuser")
    pl = (out or {}).get("sleep_plan")
    # 只断言「落盘了且是她这个夜、这个时长」：bedtime 会被「不能定在过去」的防呆
    # 按当前真实钟点顺推（23:05 跑到这个测试时 23.0 会变成 23.33），别跟着钟点飘
    check("16.1 job 落 sleep_plan",
          isinstance(pl, dict) and abs(pl.get("dur_h", 0) - 6.5) < 1e-6
          and pl.get("night_key") == HormoneSystem(cfg)._night_key(time.time())
          and pl.get("source") == "model", f"plan={pl}")

    calls = {"n": 0}
    async def count_plan(config, system, report=None):
        calls["n"] += 1
        return {"bedtime": 23.0, "duration_h": 6.0, "note": "", "source": "model"}
    hp._generate_sleep_plan = count_plan
    try:
        inst2 = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin); inst2.config = cfg
        await inst2._consolidate_job()
    finally:
        hp._generate_sleep_plan = orig
    check("16.2 同夜不再重复定", calls["n"] == 0, f"calls={calls['n']}")
asyncio.run(plan_job_test())

# ---------- 17. P4 补觉 + pending 梦 ----------
cNap = cfg_consolidate(True)
sNap = HormoneSystem(cNap)
check("17.1 补觉配置默认", cNap.sleep.nap_enabled is True and abs(cNap.sleep.nap_max_hours - 2.0) < 1e-6)

tNoon = sNap._day_anchor(time.time(), 14.0)
sNap.fatigue = 80.0
sNap.sleep_debt = 3.0
sNap.last_interaction_time = tNoon - 3600
check("17.2 白天累+安静→该补觉", sNap.should_nap(tNoon) is True, f"nap={sNap.nap}")

nap = sNap.start_nap(1.0, "model", now=tNoon)
check("17.3 落地补觉", isinstance(sNap.nap, dict) and abs(nap["dur_h"] - 1.0) < 1e-6 and sNap.nap_date == sNap._today_key(tNoon))
check("17.4 当天不再重复建议", sNap.should_nap(tNoon) is False)

sCool = HormoneSystem(cNap); sCool.fatigue = 80.0; sCool.sleep_debt = 3.0
sCool.last_interaction_time = tNoon - 3600
sCool.nap_check_ts = tNoon - 10 * 60
check("17.4b 询问冷却内不再问", sCool.should_nap(tNoon) is False)
sCool2 = HormoneSystem(cNap); sCool2.fatigue = 80.0; sCool2.sleep_debt = 3.0
sCool2.last_interaction_time = tNoon - 3600
sCool2.nap_check_ts = tNoon - 70 * 60
check("17.4c 冷却过可再问", sCool2.should_nap(tNoon) is True)

debt_before_nap = sNap.sleep_debt
fat_before_nap = sNap.fatigue
sNap._finalize_nap(tNoon + 1800)  # 实际睡了0.5h
check("17.5 补觉回补债/缓疲劳",
      sNap.nap is None and sNap.sleep_debt < debt_before_nap and sNap.fatigue < fat_before_nap,
      f"debt {debt_before_nap}->{sNap.sleep_debt}")

# nap 期间被吵醒（轻）
sWake = HormoneSystem(cfg_consolidate(True))
sWake.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
rn = time.time()
sWake.nap = {"start_ts": rn - 600, "wake_ts": rn + 3000, "dur_h": 1.0, "source": "model"}
sWake.last_interaction_time = rn - 3600
sWake.update_hormone_by_text("在吗")
check("17.6 nap 被吵醒(轻)", sWake._sleep_stage == "nap_wake" and sWake.nap is None, f"stage={sWake._sleep_stage}")

# pending 梦：短睡→转正
cPd = cfg_consolidate(True)
sPd = HormoneSystem(cPd)
sPd.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sPd.set_pending_dream("梦见和你散步", "sweet")
sPd._settle_last_sleep(pre, 5.0, False, time.time())
check("17.7 pending→短睡转正",
      isinstance(sPd.gen_dream, dict) and sPd.gen_dream.get("text") == "梦见和你散步"
      and len(sPd.sleep_journal) == 1 and sPd.pending_dream is None)
sPd.wake_up_turns = 0
sPd.wake_up_reason = ""
_p_prompt = "\n".join(_sleep_lines(sPd, cPd, ""))
check("17.7b 转正后的梦能进 prompt", "梦里还留着甜" in _p_prompt, _p_prompt[:50])

# pending 梦：通宵→丢弃
sPd2 = HormoneSystem(cfg_consolidate(True))
sPd2.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sPd2.set_pending_dream("通宵不该有梦", "sweet")
sPd2._settle_last_sleep(pre, 0.0, False, time.time())
check("17.8 pending→通宵丢弃", sPd2.pending_dream is None and sPd2.gen_dream is None)

# 状态往返
sRTN = HormoneSystem(cfg_consolidate(True)); sRTN.nap = {"start_ts": 1.0, "wake_ts": 2.0, "dur_h": 1.0, "source": "model"}
sRTN.nap_date = "2026-10-23"; sRTN.pending_dream = {"text": "草稿", "tone": "sweet", "recall": "fuzzy", "worth": True, "gen_ts": 1.0}
sRTN2 = HormoneSystem(cfg_consolidate(True)); sRTN2.apply_state(sRTN.to_state(3))
check("17.9 nap/pending 往返", isinstance(sRTN2.nap, dict) and isinstance(sRTN2.pending_dream, dict) and sRTN2.nap_date == "2026-10-23")

# ---------- 18. 自审小修 ----------
sSum = HormoneSystem(cfg_consolidate(True)); sSum.sleep_debt = 2.5; sSum.last_sleep_kind = "short"
sSum.apply_sleep_plan(23.5, 6.0, now=sSum._day_anchor(time.time(), 22.0))
summ = sSum.sleep_state_summary()
check("18.1 状态摘要", "今晚" in summ and "睡眠债 2.5" in summ and "睡眠质量" in summ, summ[:80])

cOff2 = cfg_consolidate(True)
lt2 = time.localtime(); h2 = lt2.tm_hour + lt2.tm_min / 60.0
cOff2.sleep.wake_time = (h2 - 0.5) % 24; cOff2.sleep.bedtime = (h2 - 6.5) % 24
sOff2 = HormoneSystem(cOff2)
sOff2.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sOff2.last_interaction_time = time.time() - 40 * 3600
sOff2.update_hormone_by_text("好久不见")
check("18.2 离线>36h不结算", sOff2.sleep_debt == 0.0 and sOff2.last_sleep_kind == "")

sPd3 = HormoneSystem(cfg_consolidate(True))
sPd3.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sPd3.pending_dream = {"text": "旧梦", "tone": "sweet", "recall": "clear", "worth": True,
                      "gen_ts": time.time() - 30 * 3600}
sPd3._settle_last_sleep(pre, 5.0, False, time.time())
check("18.3 pending超24h丢弃", sPd3.gen_dream is None and sPd3.pending_dream is None)

# ---------- 19. [State] 日志节流 ----------
cL = cfg_consolidate(True)
pL = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
pL._state_sig = {}; pL._state_ts = {}
tL = 1000.0
check("19.1 首次必打", pL._should_log_state("s", "A", tL, cL) is True)
check("19.2 同状态未到心跳不打", pL._should_log_state("s", "A", tL + 60, cL) is False)
check("19.3 状态变化立即打", pL._should_log_state("s", "B", tL + 120, cL) is True)
check("19.4 到心跳再打", pL._should_log_state("s", "B", tL + 120 + 30 * 60, cL) is True)
cL0 = cfg_consolidate(True); cL0.sleep.state_log_minutes = 0
pL0 = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
pL0._state_sig = {}; pL0._state_ts = {}
pL0._should_log_state("x", "A", 0.0, cL0)
check("19.5 心跳0=只打变化", pL0._should_log_state("x", "A", 99999.0, cL0) is False)

# ---------- 20. 通宵结算 / 首启跳过 ----------
cA = cfg_consolidate(True)
lt3 = time.localtime(); h3 = lt3.tm_hour + lt3.tm_min / 60.0
cA.sleep.wake_time = (h3 - 0.5) % 24; cA.sleep.bedtime = (h3 - 6.5) % 24
wake_ts2 = HormoneSystem(cA)._day_anchor(time.time(), cA.sleep.wake_time)
sAl = HormoneSystem(cA)
sAl.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sAl._state_loaded = True
sAl.last_interaction_time = wake_ts2 + 5 * 60
sAl.update_hormone_by_text("天都亮了")
check("20.1 通宵也结算", sAl.last_sleep_kind == "allnighter" and sAl.sleep_debt > 0.0,
      f"kind={sAl.last_sleep_kind} debt={sAl.sleep_debt}")

sFr = HormoneSystem(cA)
sFr.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sFr.last_interaction_time = wake_ts2 + 5 * 60
sFr.update_hormone_by_text("第一次来")
check("20.2 首启无历史不结算", sFr.sleep_debt == 0.0 and sFr.last_sleep_kind == "")

# ---------- 21. v0.4 全字段状态往返 ----------
cAll = cfg_consolidate(True)
sAll = HormoneSystem(cAll)
sAll.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin", "serotonin", "endorphin", "empathy")}
sAll.sleep_debt = 3.3; sAll.last_sleep_kind = "catchup"
sAll.sleep_plan = {"night_key": "2026-10-23", "bedtime": 23.5, "dur_h": 7.0,
                   "wake": 6.5, "decided_at": 1.0, "source": "model", "note": "n"}
sAll.nap = {"start_ts": 1.0, "wake_ts": 2.0, "dur_h": 1.0, "source": "model"}
sAll.nap_date = "2026-10-23"; sAll.nap_check_ts = 1.5
sAll.pending_dream = {"text": "草稿", "tone": "sweet", "recall": "fuzzy", "worth": True, "gen_ts": 1.0}
sAll.gen_dream = {"text": "旧梦", "tone": "neutral", "recall": "clear", "worth": True, "ts": 2.0}
sAll.recent_turns = [{"text": "a", "score": 0.2, "ts": 1.0}]
sAll.sleep_journal = [{"date": "2026-10-23", "text": "j"}]
stAll = sAll.to_state(3)
sAll2 = HormoneSystem(cfg_consolidate(True)); sAll2.apply_state(stAll)
check("21.1 v0.4 全字段往返",
      abs(sAll2.sleep_debt - 3.3) < 1e-6 and sAll2.last_sleep_kind == "catchup"
      and isinstance(sAll2.sleep_plan, dict) and sAll2.sleep_plan.get("night_key") == "2026-10-23"
      and isinstance(sAll2.nap, dict) and sAll2.nap_check_ts == 1.5 and sAll2.nap_date == "2026-10-23"
      and isinstance(sAll2.pending_dream, dict) and isinstance(sAll2.gen_dream, dict)
      and len(sAll2.recent_turns) == 1 and len(sAll2.sleep_journal) == 1
      and sAll2._state_loaded is True)

# ---------- 22. 醒着不生成梦 ----------
async def awake_job_test():
    tmpd = tempfile.mkdtemp(prefix="hormone_awake_")
    cfg = cfg_consolidate(True, task="hormone_sleep")
    cfg.core.state_dir = tmpd
    lt = time.localtime(); hour = lt.tm_hour + lt.tm_min / 60.0
    cfg.sleep.bedtime = (hour - 4.0) % 24.0
    cfg.sleep.wake_time = (hour + 4.0) % 24.0
    store = HormoneStateStore(tmpd)
    seed = HormoneSystem(cfg)
    seed.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 85.0,
                     "serotonin": 70.0, "endorphin": 75.0, "empathy": 60.0}
    seed.emotion_memory = [(0.8, time.time())]
    seed.recent_turns = [{"text": "聊着呢", "score": 0.5, "ts": time.time()}]
    seed.last_interaction_time = time.time() - 5 * 60  # 刚聊过 = 醒着
    store.save("awake", seed.to_state(3))
    calls = {"n": 0}
    async def fake(config, items, tone, report=None):
        calls["n"] += 1
        return {"dream": "不该有", "recall": "fuzzy", "worth": False}
    orig = hp._generate_dream
    hp._generate_dream = fake
    try:
        inst = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
        inst.config = cfg
        await inst._consolidate_job()
    finally:
        hp._generate_dream = orig
    check("22.1 醒着不生成梦", calls["n"] == 0, f"calls={calls['n']}")
asyncio.run(awake_job_test())

# ---------- 23. 模型任务兜底链 + 梦失败要看得见 ----------
import datetime as _dtm
from zoneinfo import ZoneInfo as _ZI
import src.app.plugin_system.api.llm_api as _api

_tz23 = _ZI("Asia/Shanghai")
def _ts23(day, hour, minute=0):
    return _dtm.datetime(2026, 10, day, hour, minute, tzinfo=_tz23).timestamp()

_real_time23 = time.time
_c23 = [0.0]
time.time = lambda: _c23[0]

def _cfg23(state_dir, timeout=5.0, total=None):
    c = cfg_consolidate(True, task="")
    c.core.state_dir = state_dir
    c.sleep.timezone = "Asia/Shanghai"
    c.sleep.consolidate_timeout_seconds = timeout
    if total is not None:
        c.sleep.consolidate_total_seconds = total
    return c

def _bare23(cfg):
    i = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
    i.config = cfg
    i._schedule_ids = []
    i._register_task_id = None
    i._state_sig = {}
    i._state_ts = {}
    i._sleep_notes = {}
    return i

def _seed23(cfg, store, sid, quiet_min=600, turns=1):
    s = HormoneSystem(cfg)
    s.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 85.0,
                  "serotonin": 70.0, "endorphin": 75.0, "empathy": 60.0}
    s.emotion_memory = [(0.8, _c23[0])]
    s.recent_turns = [{"text": "他说最喜欢你", "score": 0.8, "ts": _c23[0]}] * turns
    s.last_interaction_time = _c23[0] - quiet_min * 60
    s.last_sleep_settle = _ts23(22, 7, 0)
    store.save(sid, s.to_state(cfg.core.state_version))
    return s

class _Cap23:
    """假 logger：把插件说的话按级别收起来，方便断言「说了什么、说了几次」。"""
    def __init__(self):
        self.lines = []
    def info(self, m, *a, **k): self.lines.append(("info", str(m)))
    def warning(self, m, *a, **k): self.lines.append(("warning", str(m)))
    def debug(self, m, *a, **k): self.lines.append(("debug", str(m)))
    def said(self, needle, lvl="info"):
        return [t for (l, t) in self.lines if l == lvl and needle in t]

class _FakeReq23:
    def __init__(self, text=None, exc=None, slow=0.0):
        self._text, self._exc, self._slow = text, exc, slow
    def add_payload(self, p): pass
    async def send(self, stream=False):
        if self._exc:
            raise self._exc
        if self._slow:            # 慢在请求本身：外面的 wait_for 才该掐掉它
            await asyncio.sleep(self._slow)
        text = self._text
        async def _resolve():
            return text
        return _resolve()

def _dead(name):
    raise ValueError(f"任务 '{name}' 无可用渠道")

# 23.1–23.4 纯函数
c23a = HormoneSystemConfig()
check("23.1 默认兜底链=actor起依次备用",
      hp._consolidate_tasks(c23a) == ("actor", "sub_actor", "utils_small", "utils"),
      str(hp._consolidate_tasks(c23a)))
c23b = cfg_consolidate(True, task="hormone_sleep")
check("23.2 配置任务排最前且不重复",
      hp._consolidate_tasks(c23b)[0] == "hormone_sleep"
      and len(hp._consolidate_tasks(c23b)) == len(set(hp._consolidate_tasks(c23b))))
c23c = cfg_consolidate(True, task="actor")
check("23.3 配了 actor 也只出现一次", list(hp._consolidate_tasks(c23c)).count("actor") == 1)
check("23.4 空异常也能说清/过长会截", hp._reason("") == "无详细信息" and hp._reason("x" * 300).endswith("…"))

_O_GMS, _O_CLR = _api.get_model_set_by_task, _api.create_llm_request
_O_LOG = hp.logger

async def chain_tests():
    # A：actor 挂了 → 备用任务把梦做出来
    seen = []
    def gms_failover(name):
        seen.append(name)
        if name == "actor":
            _dead(name)
        return ["fake"]
    _api.get_model_set_by_task = gms_failover
    _api.create_llm_request = lambda ms, rn: _FakeReq23(text='{"dream":"梦见和你走在夜路上"}')
    rep = {}
    out = await hp._generate_dream(
        _cfg23(tempfile.mkdtemp(prefix="hormone_chain_")),
        [{"text": "他说最喜欢你", "score": 0.8, "ts": 0.0}], "sweet", report=rep
    )
    check("23.5 actor挂→备用任务仍做出梦",
          bool(out) and out["dream"] == "梦见和你走在夜路上"
          and seen[:2] == ["actor", "sub_actor"] and not rep.get("error"), f"seen={seen}")

    # B：全都不可用 → None + 原因里带任务名
    rep2 = {}
    _api.get_model_set_by_task = _dead
    out2 = await hp._generate_dream(
        _cfg23(tempfile.mkdtemp(prefix="hormone_chain2_")),
        [{"text": "x", "score": 0.8, "ts": 0.0}], "sweet", report=rep2
    )
    check("23.6 全挂→None且原因可诊断",
          out2 is None and "actor" in rep2.get("error", "") and "sub_actor" in rep2["error"],
          rep2.get("error"))

    # C：单次预算用完后换下一个任务，但总预算兜住，不拖成长任务
    rep3 = {}
    _api.get_model_set_by_task = lambda name: ["fake"]
    cfg_slow = _cfg23(tempfile.mkdtemp(prefix="hormone_slow_"), timeout=2.0, total=4.0)
    _api.create_llm_request = lambda ms, rn: _FakeReq23(slow=2.4)
    t0 = time.monotonic()
    out3 = await hp._generate_dream(cfg_slow, [{"text": "x", "score": 0.8, "ts": 0.0}], "sweet", report=rep3)
    spent = time.monotonic() - t0
    check("23.7 单次超时换下一个任务·总预算兜住",
          out3 is None and 3.5 <= spent < 7.0 and "总时间预算" in rep3.get("error", "")
          and rep3["error"].count("TimeoutError") >= 2,
          f"spent={spent:.1f}s err={rep3.get('error')}")

    # D：熟睡中段 + 模型全挂 → 一行「没成」，且同一晚只说一句
    cfg_d = _cfg23(tempfile.mkdtemp(prefix="hormone_dreamfail_"))
    store_d = HormoneStateStore(cfg_d.core.state_dir)
    _c23[0] = _ts23(24, 3, 0)
    _seed23(cfg_d, store_d, "fail")
    _api.get_model_set_by_task = lambda name: _dead(name)
    cap = _Cap23()
    hp.logger = cap
    inst = _bare23(cfg_d)
    try:
        await inst._consolidate_job()
        _c23[0] = _ts23(24, 3, 10)
        await inst._consolidate_job()
    finally:
        hp.logger = _O_LOG
    out_d = store_d.load("fail") or {}
    said = cap.said("熟睡里想酿一个梦，没成")
    check("23.8 梦没做成要说出来", len(said) == 1 and "无可用渠道" in said[0], f"lines={said}")
    check("23.9 同一晚同一原因只说一句", len(cap.said("没成")) == 1, f"all={len(cap.said('没成'))}")
    check("23.10 失败不落 pending", out_d.get("pending_dream") is None and out_d.get("recent_turns"))

    # E：闸门挡住的两种原因，各说一句人话
    async def _blocked(hour_min, quiet, turns, tag):
        cfg_e = _cfg23(tempfile.mkdtemp(prefix="hormone_blk_"))
        store_e = HormoneStateStore(cfg_e.core.state_dir)
        _c23[0] = _ts23(24, 3, hour_min)
        _seed23(cfg_e, store_e, tag, quiet_min=quiet, turns=turns)
        cap_e = _Cap23()
        hp.logger = cap_e
        try:
            await _bare23(cfg_e)._consolidate_job()
        finally:
            hp.logger = _O_LOG
        return cap_e.said("今晚不做梦")

    got = await _blocked(0, 5, 1, "chatty")
    check("23.11 还在聊→不做梦并说明", len(got) == 1 and "还在聊" in got[0], f"{got}")
    got2 = await _blocked(0, 600, 0, "noitem")
    check("23.12 没原料→不做梦并说明", len(got2) == 1 and "能入梦的事" in got2[0], f"{got2}")

    # F：备用任务成功 → [DreamPend] 落盘
    cfg_f = _cfg23(tempfile.mkdtemp(prefix="hormone_dreamok_"))
    store_f = HormoneStateStore(cfg_f.core.state_dir)
    _c23[0] = _ts23(24, 3, 0)
    _seed23(cfg_f, store_f, "ok")
    _api.get_model_set_by_task = lambda name: _dead(name) if name == "actor" else ["fake"]
    _api.create_llm_request = lambda ms, rn: _FakeReq23(text='{"dream":"梦见你把星星装进罐子"}')
    cap_f = _Cap23()
    hp.logger = cap_f
    try:
        await _bare23(cfg_f)._consolidate_job()
    finally:
        hp.logger = _O_LOG
    out_f = store_f.load("ok") or {}
    pd_f = out_f.get("pending_dream") or {}
    check("23.13 备用任务成梦→[DreamPend]",
          pd_f.get("text") == "梦见你把星星装进罐子" and bool(cap_f.said("DreamPend"))
          and bool(cap_f.said("换用备用模型任务")), f"pd={pd_f}")
    check("23.14 成功后清空原料（今晚不再做）", out_f.get("recent_turns") == [])
asyncio.run(chain_tests())

_api.get_model_set_by_task, _api.create_llm_request = _O_GMS, _O_CLR
hp.logger = _O_LOG
time.time = _real_time23

# ---------- 24. 收尾期要知道她自己定的钟点（冻结时钟，别跟着真实钟点飘） ----------
from hormone_system.prompt import _sleep_tone_line, _SLEEP_TONE, MAX_PROMPT_CHARS

_real_time24 = time.time
_now24 = _dtm.datetime(2026, 10, 23, 23, 20, tzinfo=_tz23).timestamp()
time.time = lambda: _now24

cW24 = cfg_consolidate(True)
sW24 = HormoneSystem(cW24)
sW24.hormones = {"dopamine": 100.0, "cortisol": 71.0, "oxytocin": 100.0,
                 "serotonin": 100.0, "endorphin": 100.0, "empathy": 100.0}
sW24.sleep_plan = {"night_key": sW24._night_key(_now24), "bedtime": 23.667,
                   "dur_h": 6.5, "wake": 6.167, "decided_at": _now24,
                   "source": "model", "note": ""}
_tl_plan = _sleep_tone_line(sW24, "wind_down")
check("24.1 定过作息→收尾提示带上她自己定的钟点",
      "23:40" in _tl_plan and "道晚安" in _tl_plan, _tl_plan)
check("24.2 没定作息→仍是通用收尾文案",
      _sleep_tone_line(HormoneSystem(cW24), "wind_down") == _SLEEP_TONE["wind_down"])
check("24.3 其它相位文案没被动过",
      _sleep_tone_line(sW24, "sleeping") == _SLEEP_TONE["sleeping"]
      and _sleep_tone_line(sW24, "groggy") == _SLEEP_TONE["groggy"]
      and _sleep_tone_line(sW24, "") == "")
check("24.3b 冻结在 23:20 时相位确为收尾", sW24.current_sleep_phase(_now24) == "wind_down")
_p24 = build_mood_prompt(sW24, cW24)
check("24.4 加长后的收尾行仍进得了注入预算",
      "23:40" in _p24 and len(_p24) <= MAX_PROMPT_CHARS, f"len={len(_p24)}")
time.time = _real_time24
check("24.5 收尾行里不出现激素名/梦字（不与旧约束打架）",
      not any(h in _tl_plan for h in NO_H) and "梦" not in _tl_plan)
check("24.6 收尾期默认只提前半小时（哪有犯困一个半小时的）",
      HormoneSystemConfig().sleep.wind_down_hours == 0.5)
c24b = cfg_consolidate(True)  # bedtime 23.5，wind_down 用新默认 0.5
s24b = HormoneSystem(c24b)
_bd24 = HormoneSystem(c24b)._day_anchor(time.time(), c24b.sleep.bedtime)
check("24.7 提前 50 分钟不再算收尾",
      s24b.current_sleep_phase(_bd24 - 50 * 60) != "wind_down",
      s24b.current_sleep_phase(_bd24 - 50 * 60))
check("24.8 提前 20 分钟才进收尾",
      s24b.current_sleep_phase(_bd24 - 20 * 60) == "wind_down",
      s24b.current_sleep_phase(_bd24 - 20 * 60))

# ---------- 25. 入梦原料：剥壳留名 + 长度 ----------
_norm25 = hp._normalize_relay_text
_env25a = ("[新消息]\n》2026-10-24 13:52:46》[QQ:2750694203] 中生 "
           "[消息id:532868125]： 早啊小小今昔")
check("25.1 转发信封压成「昵称：正文」", _norm25(_env25a) == "中生：早啊小小今昔", _norm25(_env25a))
_env25b = ("[新消息]\n》2026-10-24 20:40:00》[QQ:2750694203] 中生 [消息id:532868126]： "
           "[回复<你(1323044945)：@<中生:2750694203>  对话太密了]，说：顺便再给今昔做一个刷b站的插件")
check("25.2 引用壳剥掉、留真正说出口的那句",
      _norm25(_env25b) == "中生：顺便再给今昔做一个刷b站的插件", _norm25(_env25b))
check("25.3 普通消息原样返回（不误伤普通冒号）",
      _norm25("今天的天气：很好") == "今天的天气：很好"
      and _norm25("") == "" and _norm25("随便聊聊") == "随便聊聊")
_long25 = "很长的昵称很长的昵称很长的昵称"
check("25.4 昵称过长截到 12 字",
      _norm25(f"[QQ:1] {_long25} [消息id:9]： 在吗") == f"{_long25[:12]}：在吗")
_blank25 = _norm25("[新消息]》2026-10-24 13:52:46》[QQ:1] 中生 [消息id:9]： ")
check("25.5 只有信封没正文→空串（不把壳当原料）", _blank25 == "", _blank25)
_half25 = _norm25("[新消息]》2026-10-24 13:52:46》[QQ:1] 中生 [消息id:9]： [回复<你(1323044945")
check("25.5b 被截断的半截引用壳→空串", _half25 == "", _half25)

cfg25 = cfg_consolidate(True)
check("25.6 原料长度默认 120（可配）", cfg25.sleep.turn_clip_chars == 120)
s25 = HormoneSystem(cfg25)
_long_body = "中生：" + "好那今昔也一直在我怀里睡到现在啊，" * 8
s25.update_hormone_by_text(_long_body)
_turn25 = s25.recent_turns[-1]["text"]
check("25.7 超长才截、且带省略号", len(_turn25) == 121 and _turn25.endswith("…")
      and _turn25.startswith("中生："), f"len={len(_turn25)}")
s25b = HormoneSystem(cfg25)
s25b.update_hormone_by_text("中生：" + "好那今昔也一直在我怀里睡到现在啊")
check("25.8 一到两句完整的话不再被腰斩",
      s25b.recent_turns[-1]["text"] == "中生：好那今昔也一直在我怀里睡到现在啊",
      s25b.recent_turns[-1]["text"])

_full25 = ("刚刚做了个很长的梦，梦里我们走了好久的夜路，路灯一盏一盏亮着，"
           "你一直牵着我的手没松开，风把外套吹得鼓起来，我靠着你走了一路才醒")
_vals25b = {"content": [{"type": "text",
                         "text": f"[新消息]\n》2026-10-24 23:10:00》[QQ:2750694203] 中生 [消息id:777]： {_full25}"}]}
_msg25 = hp.HormonePromptHandler._extract_message_text(_vals25b)
s25c = HormoneSystem(cfg25)
s25c.update_hormone_by_text(_msg25)
_stored25 = s25c.recent_turns[-1]["text"]
check("25.8b 长消息完整入原料（旧版只剩前 17 字）",
      _stored25 == f"中生：{_full25}", f"len={len(_stored25)}")

_vals25 = {"content": [{"type": "text", "text": _env25a + "\n\n---\n回复要短"}]}
check("25.9 走事件路径也剥壳（last-mile 一起处理）",
      hp.HormonePromptHandler._extract_message_text(_vals25) == "中生：早啊小小今昔")

# 25.10 同一夜同一闸门只说一句（分钟数不能再被当成"新原因"）
_real25 = time.time
async def throttle_night():
    cfg25c = _cfg23(tempfile.mkdtemp(prefix="hormone_thr_"))
    store25 = HormoneStateStore(cfg25c.core.state_dir)
    _c23[0] = _ts23(24, 3, 0)
    _seed23(cfg25c, store25, "thr", quiet_min=5, turns=1)
    cap = _Cap23()
    hp.logger = cap
    try:
        inst25 = _bare23(cfg25c)
        for mins in (0, 5, 10):
            _c23[0] = _ts23(24, 3, mins)
            await inst25._consolidate_job()
    finally:
        hp.logger = _O_LOG
    return cap.said("今晚不做梦")
time.time = lambda: _c23[0]
try:
    _lines25 = asyncio.run(throttle_night())
finally:
    time.time = _real25
check("25.10 同一夜同一闸门只说一句", len(_lines25) == 1 and "还在聊" in _lines25[0],
      f"{len(_lines25)}: {_lines25[:2]}")

# ---------- 26. 注入预算：睡眠段优先于单条激素行 ----------
c26 = cfg_consolidate(True)
s26 = HormoneSystem(c26)
s26.hormones = {"dopamine": 100.0, "cortisol": 100.0, "oxytocin": 100.0,
                "serotonin": 100.0, "endorphin": 100.0, "empathy": 100.0}
s26.fatigue = 100.0
s26.sleep_quality = "good"
s26.gen_dream = {"text": "梦见和你走在夜路上", "tone": "sweet", "recall": "fuzzy",
                 "worth": True, "ts": time.time()}
_p26 = build_mood_prompt(s26, c26)
check("26.1 激素行再挤，『梦里还留着甜』也不会被挤掉",
      "梦里还留着甜" in _p26 and len(_p26) <= MAX_PROMPT_CHARS, f"len={len(_p26)}")
check("26.2 预算没被撑爆且仍有激素/状态行",
      len(_p26.splitlines()) >= 5 and ("疲劳" in _p26 or "累" in _p26), _p26[:60])

# ---------- 27. 中断标记只对本夜有效（不能跨夜污染） ----------
_c27 = [0.0]
_real27 = time.time
time.time = lambda: _c27[0]
c27 = cfg_consolidate(True)
c27.sleep.timezone = "Asia/Shanghai"
try:
    # A：昨夜残留的标记 → 今晚该清掉，而且今晚被吵醒照常有激素反应
    _c27[0] = _ts23(24, 3, 0)
    s27 = HormoneSystem(c27)
    s27.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin",
                                      "serotonin", "endorphin", "empathy")}
    s27.hormones["oxytocin"] = 60.0
    s27.sleep_interrupted_at = _ts23(23, 2, 0)      # 昨夜 10-23 02:00 残留
    _pre27 = dict(s27.hormones)
    _stage27 = s27._apply_sleep(_pre27, _ts23(24, 0, 30))  # 上一条 00:30，安静 2.5h
    check("27.1 昨夜残留的中断标记会被清掉",
          abs(s27.sleep_interrupted_at - _c27[0]) < 1e-6, s27.sleep_interrupted_at)
    check("27.2 清掉后今晚被吵醒照样扣激素",
          _stage27 == "night_wake"
          and s27.hormones["cortisol"] > _pre27["cortisol"] + 5,
          f"stage={_stage27} cor={s27.hormones['cortisol']:.0f}")

    # B：本夜已经醒过一次 → 标记保留、不重复扣
    _c27[0] = _ts23(24, 4, 0)
    s28 = HormoneSystem(c27)
    s28.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin",
                                      "serotonin", "endorphin", "empathy")}
    s28.sleep_interrupted_at = _ts23(24, 1, 0)      # 本夜 01:00 已经醒过一次
    _before28 = s28.sleep_interrupted_at
    _pre28 = dict(s28.hormones)
    _stage28 = s28._apply_sleep(_pre28, _ts23(24, 3, 30))
    check("27.3 本夜的标记保留、不重复扣激素",
          _stage28 == "night_wake" and s28.sleep_interrupted_at == _before28
          and abs(s28.hormones["cortisol"] - _pre28["cortisol"]) < 0.01,
          f"stage={_stage28} ts={s28.sleep_interrupted_at}")
finally:
    time.time = _real27

# 27.4/27.5 过期草稿别在 [State] 里假报"待确认"
_c27b = cfg_consolidate(True)
s29 = HormoneSystem(_c27b)
s29.pending_dream = {"text": "旧草稿", "tone": "sweet", "gen_ts": time.time() - 25 * 3600}
_sum29 = s29.sleep_state_summary()
check("27.4 超 24h 的草稿不再说'有梦待晨间确认'",
      "待晨间确认" not in _sum29 and "散了" in _sum29, _sum29)
s30 = HormoneSystem(_c27b)
s30.pending_dream = {"text": "草稿", "tone": "sweet", "gen_ts": time.time() - 3600}
check("27.5 本夜的草稿仍显示待确认", "有梦待晨间确认" in s30.sleep_state_summary())

# ---------- 28. 死草稿清理（>24h 没等到结算，别一直躺在状态文件里） ----------
c28 = cfg_consolidate(True)
s31 = HormoneSystem(c28)
s31.pending_dream = {"text": "旧", "tone": "sweet", "gen_ts": time.time() - 25 * 3600}
check("28.1 超 24h → 丢掉并返回 True",
      s31.drop_expired_pending() is True and s31.pending_dream is None)
s32 = HormoneSystem(c28)
s32.pending_dream = {"text": "新", "tone": "sweet", "gen_ts": time.time() - 2 * 3600}
check("28.2 本夜的草稿不动",
      s32.drop_expired_pending() is False and isinstance(s32.pending_dream, dict))
check("28.3 没有草稿也不报错", HormoneSystem(c28).drop_expired_pending() is False)

_real28 = time.time
async def expired_job():
    cfg = _cfg23(tempfile.mkdtemp(prefix="hormone_pd_"))
    cfg.sleep.dynamic_schedule = False  # 关掉定作息，只测清理
    store = HormoneStateStore(cfg.core.state_dir)
    _c23[0] = _ts23(24, 14, 0)
    s = _seed23(cfg, store, "pd")
    s.pending_dream = {"text": "旧草稿", "tone": "sweet", "gen_ts": _ts23(23, 2, 0)}
    store.save("pd", s.to_state(cfg.core.state_version))
    cap = _Cap23()
    hp.logger = cap
    try:
        await _bare23(cfg)._consolidate_job()
    finally:
        hp.logger = _O_LOG
    return (store.load("pd") or {}).get("pending_dream"), cap.said("草稿丢了")
time.time = lambda: _c23[0]
try:
    _pd_left, _pd_note = asyncio.run(expired_job())
finally:
    time.time = _real28
check("28.4 后台任务在梦窗之外也会清掉并说一句",
      _pd_left is None and len(_pd_note) == 1, f"{_pd_left} {_pd_note}")

# ---------- 29. 梦料机制（双方发言 / 跨天分层 / 冲突配额 / 片段 / 基调下限） ----------
import types as _types

def _cfg29(**over):
    c = cfg_consolidate(True)
    c.core.state_dir = tempfile.mkdtemp(prefix="hormone_mat_")
    for k, v in over.items():
        setattr(c.sleep, k, v)
    return c

def _seed_turns(s, items):
    """items: [(text, score, hours_ago, who)]；内部按时间正序排（老的在前），跟真实追加顺序一致。"""
    now = time.time()
    s.recent_turns = [
        {"text": t, "score": sc, "ts": now - h * 3600, "who": w}
        for t, sc, h, w in sorted(items, key=lambda x: -x[2])
    ]

def _all_text(frags):
    return " | ".join(f["text"] for f in frags)

# 29.1/29.2 她的话进梦料，片段里双方并列
c29 = _cfg29()
s29 = HormoneSystem(c29)
s29.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 85.0,
                "serotonin": 70.0, "endorphin": 75.0, "empathy": 60.0}
s29.emotion_memory = [(0.8, time.time())]
_seed_turns(s29, [("他说最喜欢你", 0.8, 1, "user")])
s29.record_self_turn("我一直都在")
check("29.1 她自己的话能进梦料", s29.recent_turns[-1].get("who") == "self"
      and s29.recent_turns[-1]["text"] == "我一直都在", s29.recent_turns[-1])
_frags29, _t29 = s29.get_generation_input()
check("29.2 片段里双方并列（他：…／我：…）",
      "他：他说最喜欢你" in _all_text(_frags29) and "我：我一直都在" in _all_text(_frags29),
      _all_text(_frags29))

# 29.3 上限 120 条 + 只留两天
c29b = _cfg29()
s29b = HormoneSystem(c29b)
now29 = time.time()
s29b.recent_turns = [
    {"text": f"m{i}", "score": 0.0, "ts": now29 - i * 60, "who": "user"}
    for i in range(300)
]
s29b.recent_turns.append({"text": "三天前", "score": 0.0, "ts": now29 - 72 * 3600, "who": "user"})
s29b._push_recent_turn("最新一句", 0.2)
check("29.3 梦料上限 120 且超过两天被丢",
      len(s29b.recent_turns) == 120 and "三天前" not in [t["text"] for t in s29b.recent_turns],
      len(s29b.recent_turns))

# 29.4 冲突配额：最近 24h 的强负面必须入选（哪怕别的分更高）
c29c = _cfg29(dream_random_picks=0)
s29c = HormoneSystem(c29c)
s29c.hormones = {"dopamine": 70.0, "cortisol": 60.0, "oxytocin": 85.0,
                 "serotonin": 60.0, "endorphin": 70.0, "empathy": 70.0}
s29c.emotion_memory = [(0.5, time.time())]
_seed_turns(s29c, [
    ("今天吵了一架", -0.8, 3, "user"),
    ("她说她很难受", -0.7, 2, "user"),
    ("我们吃了很好吃的饭", 0.9, 8, "user"),
    ("他又逗我笑", 0.9, 9, "user"),
    ("明天见", 0.2, 1, "user"),
    ("晚安", 0.1, 0.5, "user"),
])
_f29c, _ = s29c.get_generation_input()
check("29.4 强负面有强制配额", "今天吵了一架" in _all_text(_f29c), _all_text(_f29c))

# 29.5 未消解的冲突 → 压成甜虐，不给纯甜
check("29.5 吵架没被哄好 → 基调封顶 ambivalent", _ == "ambivalent", _)

# 29.6 被哄好了（之后有 ≥0.6 的正面）→ 允许纯甜
c29d = _cfg29(dream_random_picks=0)
s29d = HormoneSystem(c29d)
s29d.hormones = {"dopamine": 80.0, "cortisol": 30.0, "oxytocin": 85.0,
                 "serotonin": 75.0, "endorphin": 75.0, "empathy": 65.0}
s29d.emotion_memory = [(0.8, time.time())]
_seed_turns(s29d, [
    ("今天吵了一架", -0.8, 4, "user"),
    ("后来他好好道歉了，抱了好久", 0.8, 1, "user"),
])
_f29d, _t29d = s29d.get_generation_input()
check("29.6 冲突被消解后可以做甜梦", _t29d == "sweet", _t29d)

# 29.7 时间分层：三个时间段各有一条被带上
c29e = _cfg29(dream_random_picks=0, top_k=3, dream_context_lines=0)
s29e = HormoneSystem(c29e)
s29e.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 60.0,
                 "serotonin": 70.0, "endorphin": 70.0, "empathy": 60.0}
_seed_turns(s29e, [
    ("今晚的高分话", 0.95, 0.5, "user"),   # 第 1 层
    ("今天下午的话", 0.90, 10, "user"),    # 第 2 层里最显著的
    ("昨晚的话", 0.10, 20, "user"),        # 第 2 层里不显著的（不该挤掉下午那条）
    ("昨天的话", 0.10, 30, "user"),        # 第 3 层（纯按分数排它进不了 top3）
])
_f29e, _ = s29e.get_generation_input()
_txt29e = _all_text(_f29e)
check("29.7 三段时间各有一条入梦（不纯按分数）",
      "今晚的高分话" in _txt29e and "今天下午的话" in _txt29e
      and "昨天的话" in _txt29e and "昨晚的话" not in _txt29e,
      _txt29e)

# 29.8 片段化 + 字数上限
check("29.8 片段带说话人、整块不超 620 字",
      all(("他：" in f["text"] or "我：" in f["text"]) for f in _frags29)
      and sum(len(f["text"]) for f in _frags29) <= 620,
      sum(len(f["text"]) for f in _frags29))

# 29.9 prompt 里写清了"梦是碎的、不要复述、别只挑甜的"
check("29.9 梦提示词含三条梦化约束",
      "不要照抄原句" in hp._DREAM_PROMPT
      and "不要总结" in hp._DREAM_PROMPT
      and "不要只挑好的一面" in hp._DREAM_PROMPT)

# 29.10 她的发言通过 on_message_sent 落盘；占位消息不记录；无状态的流不新建
async def sent_handler_test():
    c = _cfg29()
    store = HormoneStateStore(c.core.state_dir)
    s = HormoneSystem(c)
    s.hormones = {"dopamine": 50.0, "cortisol": 50.0, "oxytocin": 50.0,
                  "serotonin": 50.0, "endorphin": 50.0, "empathy": 50.0}
    store.save("S1", s.to_state(c.core.state_version))
    inst = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
    inst.config = c
    h = hp.HormoneSentHandler.__new__(hp.HormoneSentHandler)
    h.plugin = inst
    await h.execute("on_message_sent", {"message": _types.SimpleNamespace(
        stream_id="S1", processed_plain_text="我一直都在，别怕", content=None)})
    await h.execute("on_message_sent", {"message": _types.SimpleNamespace(
        stream_id="S1", processed_plain_text="[表情包(abc):某个表情]", content=None)})
    await h.execute("on_message_sent", {"message": _types.SimpleNamespace(
        stream_id="NOPE", processed_plain_text="没有状态文件", content=None)})
    return store, c
_store29, _c29 = asyncio.run(sent_handler_test())
_st29 = _store29.load("S1") or {}
_turns29 = _st29.get("recent_turns") or []
check("29.10 发出的文本记成 who=self", len(_turns29) == 1
      and _turns29[0]["who"] == "self" and "我一直都在" in _turns29[0]["text"], _turns29)
check("29.11 占位消息不记录、没状态的流不新建",
      len(_turns29) == 1 and not _store29.load("NOPE"))

# 29.12 老状态没有 who → 视为 user；往返保留 who
_old = HormoneSystem(_cfg29())
_old.apply_state({"version": 3, "recent_turns": [{"text": "老数据", "score": 0.3, "ts": time.time()}]})
check("29.12 旧状态下 who 默认 user", _old.recent_turns[0].get("who") == "user")
_rt29 = HormoneSystem(_cfg29())
_rt29.recent_turns = [{"text": "a", "score": 0.0, "ts": time.time(), "who": "self"}]
_rt29b = HormoneSystem(_cfg29())
_rt29b.apply_state(_rt29.to_state(4))
check("29.13 往返保留 self 标记", _rt29b.recent_turns[0].get("who") == "self")
_trunc = hp._coerce_dream(None, '{"dream":"他摸着我头，却捂住嘴说忘了。我变成一串乱码')
check("29.14 JSON 被截断也能把梦抠回来",
      _trunc is not None and "乱码" in _trunc["dream"], _trunc)
check("29.15 连 dream 字段都没有仍然 None", hp._coerce_dream(None, '{"recall":"clear"}') is None)
# 29.16 被她自己说过的长回复（截断的）显著性减半
c29f = _cfg29()
s29f = HormoneSystem(c29f)
s29f.record_self_turn("好" * 300)   # 会被截断
_s29f_turn = s29f.recent_turns[-1]
check("29.16 截断的她的话显著性减半",
      _s29f_turn.get("clipped") is True
      and abs(s29f._turn_salience(_s29f_turn) - 0.15) < 1e-9,
      (_s29f_turn.get("clipped"), s29f._turn_salience(_s29f_turn)))
check("29.16b 完整的一句不降权", abs(s29f._turn_salience({"who": "self"}) - 0.30) < 1e-9)

# 29.17 相邻好几条被选中时，片段会被切开（不把一晚吞成一大段）
c29g = _cfg29(dream_random_picks=0, top_k=5, dream_context_lines=1)
s29g = HormoneSystem(c29g)
s29g.hormones = {"dopamine": 70.0, "cortisol": 30.0, "oxytocin": 60.0,
                 "serotonin": 70.0, "endorphin": 70.0, "empathy": 60.0}
_seed_turns(s29g, [(f"第{i}句", 0.5, 1 - i * 0.01, "user") for i in range(8)])
_f29g, _ = s29g.get_generation_input()
check("29.17 相邻多条 → 切成多个 ≤4 句的片段", len(_f29g) >= 2
      and all(f["text"].count("／") <= 3 for f in _f29g),
      [f["text"].count("／") + 1 for f in _f29g])

# ---------- 30. 一晚没睡好 ≠ 连着几天（睡眠债文案） ----------
c30 = cfg_consolidate(True)
s30a = HormoneSystem(c30)
s30a.hormones = {k: 50.0 for k in ("dopamine", "cortisol", "oxytocin",
                                   "serotonin", "endorphin", "empathy")}
s30a._state_loaded = True
s30a._settle_last_sleep(dict(s30a.hormones), 1.0, False, time.time())   # 只睡 1h → short
check("30.1 短睡记一夜 streak",
      s30a.last_sleep_kind == "short" and s30a.sleep_short_streak == 1,
      (s30a.last_sleep_kind, s30a.sleep_short_streak))
s30a._settle_last_sleep(dict(s30a.hormones), 1.0, False, time.time())   # 再一晚
check("30.2 连续短睡累加", s30a.sleep_short_streak == 2, s30a.sleep_short_streak)
s30a._settle_last_sleep(dict(s30a.hormones), 7.0, False, time.time())   # 睡好了 → 归零
check("30.3 睡够一晚清零", s30a.sleep_short_streak == 0, s30a.sleep_short_streak)

from hormone_system.prompt import _debt_line as _dl30
s30b = HormoneSystem(c30)
s30b.sleep_debt = 4.1
s30b.last_sleep_kind = "short"
s30b.sleep_short_streak = 1
_l30 = _dl30(s30b, c30)
check("30.4 只一晚 → 不说“连着几天”",
      "连着几天" not in _l30 and "昨晚" in _l30, _l30)
s30b.sleep_short_streak = 3
_l30b = _dl30(s30b, c30)
check("30.5 连着几晚 → 才说“连着几天”", "连着几天" in _l30b, _l30b)

s30c = HormoneSystem(c30)
s30c.sleep_debt = 4.1
s30c.last_sleep_kind = "short"
s30c.sleep_short_streak = 2
_rt30 = HormoneSystem(c30)
_rt30.apply_state(s30c.to_state(4))
check("30.6 streak 能往返落盘", _rt30.sleep_short_streak == 2, _rt30.sleep_short_streak)
_mig30 = HormoneSystem(c30)
_mig30.apply_state({"version": 3, "sleep_debt": 4.1, "last_sleep_kind": "short"})
check("30.7 老状态缺 streak → 按昨晚类型补 1", _mig30.sleep_short_streak == 1,
      _mig30.sleep_short_streak)

# ---------- 31. 睡眠时长按“累计安静”算（半夜被摸一下头不该作废整夜） ----------
c31 = cfg_consolidate(True)
s31 = HormoneSystem(c31)
_base31 = s31._day_anchor(time.time(), 0.0)
# 真实那晚：23:15–23:26 上床前聊完，01:39 被摸头，04:56 被亲一口，计划 23:30→06:00
s31.sleep_awake_spans = [
    [_base31 + 23.43 * 3600, _base31 + 23.43 * 3600],
    [_base31 + 25.65 * 3600, _base31 + 25.65 * 3600],
    [_base31 + 28.93 * 3600, _base31 + 28.93 * 3600],
]
_q31 = s31._quiet_slept(_base31 + 22 * 3600, _base31 + 31 * 3600, 23.5, 6.0)
check("31.1 半夜两次互动不再把整夜作废", 6.0 < _q31 <= 7.05, round(_q31, 2))
s31.sleep_awake_spans = []
_q31b = s31._quiet_slept(_base31 + 22 * 3600, _base31 + 31 * 3600, 23.5, 6.0)
check("31.2 没有记录时与旧口径一致（7h 窗）", abs(_q31b - 7.0) < 0.01, round(_q31b, 2))

# 31.3 连续聊 11 分钟（区间）要扣掉这 11 分钟，而不是当成睡着
s31b = HormoneSystem(c31)
s31b.sleep_awake_spans = [[_base31 + 23.25 * 3600, _base31 + 23.43 * 3600]]  # 23:15–23:26
_q31b2 = s31b._quiet_slept(_base31 + 22 * 3600, _base31 + 31 * 3600, 23.5, 6.0)
check("31.3 连续聊天的那段被扣掉", 6.5 < _q31b2 < 7.0, round(_q31b2, 2))

# 31.4 通宵聊天：一路记 65 句（超过旧的 60 点上限）→ 合并成一段，只剩最后十几分钟算睡
s31c = HormoneSystem(c31)
_t = _base31 + 23.0 * 3600
while _t < _base31 + 29.83 * 3600:   # 23:00 → 05:50
    s31c._record_awake(_t)
    _t += 300
_q31c = s31c._quiet_slept(_base31 + 22 * 3600, _base31 + 31 * 3600, 23.5, 6.0)
check("31.4 长聊不被 60 点上限吃掉、也不会被当成睡着",
      len(s31c.sleep_awake_spans) == 1 and _q31c < 0.3,
      (len(s31c.sleep_awake_spans), round(_q31c, 2)))

# 31.5 区间能往返落盘；旧的散点字段也能读进来
s31d = HormoneSystem(c31)
s31d._record_awake_in_night(_base31 + 25.65 * 3600, 23.5, 6.0)
_rt31 = HormoneSystem(c31)
_rt31.apply_state(s31d.to_state(4))
check("31.5 醒着区间能往返落盘", len(_rt31.sleep_awake_spans) == 1, _rt31.sleep_awake_spans)
_old31 = HormoneSystem(c31)
_old31.apply_state({"version": 4, "sleep_awake_ts": [
    _base31 + 23.4 * 3600, _base31 + 23.42 * 3600, _base31 + 25.0 * 3600]})
check("31.6 旧散点字段能迁移成区间（相邻<10分钟合并）",
      len(_old31.sleep_awake_spans) == 2, _old31.sleep_awake_spans)

# ---------- 33. /睡眠状态 概览 ----------
c33 = cfg_consolidate(True)
s33 = HormoneSystem(c33)
_now33 = time.time()
s33.sleep_plan = {
    "night_key": s33._night_key(_now33), "bedtime": 23.667, "dur_h": 6.5,
    "wake": 6.167, "decided_at": _now33, "source": "model", "note": "",
}
s33.sleep_debt = 2.5
s33.last_sleep_kind = "short"
s33.sleep_short_streak = 2
s33.sleep_quality = "good"
s33.sleep_score = 88
s33.gen_dream = {"text": "梦见他", "tone": "sweet", "recall": "clear",
                 "worth": True, "ts": _now33}
s33.recent_turns = [
    {"text": "中生：在吗", "score": 0.2, "ts": _now33 - 3600, "who": "user"},
    {"text": "我：在的呀", "score": 0.0, "ts": _now33 - 3500, "who": "self"},
    {"text": "我：别熬了", "score": 0.0, "ts": _now33 - 3400, "who": "self"},
]
_st33 = hp.SleepStatusCommand._format_status(s33, c33, _now33)
check("33.1 概览含作息/债/梦/梦料",
      "23:40 睡" in _st33 and "睡眠债 2.5h" in _st33
      and "记着昨晚的梦" in _st33 and "梦料 3 条（他 1 / 我 2）" in _st33,
      _st33)
check("33.2 连续短睡超过两晚才提" , "连续短睡 2 晚" in _st33, _st33)
s33.sleep_short_streak = 1
_st33b = hp.SleepStatusCommand._format_status(s33, c33, _now33)
check("33.3 只有一晚不写连续短睡", "连续短睡" not in _st33b, _st33b)

s33b = HormoneSystem(c33)
_st33c = hp.SleepStatusCommand._format_status(s33b, c33, _now33)
check("33.4 没定作息时说清默认时点", "今晚还没定作息" in _st33c and "21:30" in _st33c, _st33c)

# 33.5 命令本体：没有状态文件时给一句人话，不抛错
async def _cmd33():
    import tempfile as _tf
    c = cfg_consolidate(True)
    c.core.state_dir = _tf.mkdtemp(prefix="status33_")
    inst = hp.HormoneSystemPlugin.__new__(hp.HormoneSystemPlugin)
    inst.config = c
    cmd = hp.SleepStatusCommand.__new__(hp.SleepStatusCommand)
    cmd.plugin = inst
    cmd.stream_id = "nope"
    return await cmd.handle_status()
_ok33, _txt33 = asyncio.run(_cmd33())
check("33.5 无状态时不抛错、给一句人话", _ok33 is True and "作息记录" in _txt33, _txt33)

print("\n==== RESULT ====")
print("FAILED:", fails if fails else "NONE")
sys.exit(1 if fails else 0)
