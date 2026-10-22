import time
import math
import json
import os
import sys
import logging
import random
from openai import OpenAI

# ========== 配置区 ==========
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

STATE_FILE = "hormone_state.json"
BASELINE = 50.0

HORMONE_KEYS = (
    "dopamine", "cortisol", "oxytocin",
    "serotonin", "endorphin", "empathy",
)
HALF_LIVES = {
    "dopamine":  12.0,
    "cortisol":   6.0,
    "oxytocin":  72.0,
    "serotonin": 48.0,
    "endorphin":  4.0,
    "empathy":   24.0,
}
DEFAULT_HALF_LIFE = 24.0

FATIGUE_HALF_LIFE = 12.0
MEMORY_HALF_LIFE_HOURS = 1.0
MAX_SINGLE_EMOTIONAL_DELTA = 25.0

ATTACHMENT_THRESHOLD = 80.0

# ========== 空闲自发情绪配置 ==========
IDLE_MIN_HOURS = 0.5                 # 至少空闲 30 分钟才触发自发波动
IDLE_MAX_COMPENSATION_HOURS = 6.0    # 单次空闲最多补偿 6 小时（避免 3 天不聊飘太远）
IDLE_DRIFT_RATE = 1.0                # 基础漂移速率（每小时 × sqrt(hours) × rate）

MAX_HISTORY = 40
PROMPT_HISTORY = 8
TOPIC_WINDOW = 3

# ========== 情感词表 ==========
AFFECTION_WORDS = ("爱你", "么么哒", "想你", "喜欢你", "最喜欢")
COMFORT_WORDS = ("抱抱", "辛苦", "没关系", "别难过", "我在", "陪着", "心疼", "乖", "照顾好自己")
EMPATHY_WORDS = ("懂我", "理解", "真诚", "说到心里", "在乎", "陪伴", "很重要", "看见你")
DISCOURAGE_WORDS = ("算了", "随便", "无所谓", "没意思", "不想聊", "随你")
REJECT_WORDS = ("不值得", "别管我", "走开", "不需要你", "离我远点")
VULNERABLE_WORDS = ("难过", "伤心", "累", "痛", "想哭", "撑不住", "崩溃", "孤独", "害怕", "焦虑")

CHRONIC_PRIORITY = ["意义缺失", "长期低落", "慢性心累", "共情倦怠", "深度依恋"]

log = logging.getLogger("ai")


# ========== 情感打分器 ==========
class SentimentScorer:
    def __init__(self):
        self.pos_words = {"棒", "可爱", "喜欢", "厉害", "好耶", "不错",
                          "爱你", "么么哒", "谢谢", "优秀", "牛",
                          "一定行", "可以的", "稳了"}
        self.neg_words = {"笨", "垃圾", "烦", "讨厌", "滚", "无语",
                          "闭嘴", "傻", "差劲", "烂", "恶心"}
        self.negations = {"不", "没", "别", "无", "非"}
        self.kind_words_with_sha = ("傻瓜", "傻乎乎", "傻笑", "小傻")
        self.puncts = set("，。！？、；：,.!?;:…~ \t\n")

    def _has_negation_nearby(self, prefix: str) -> bool:
        for neg in self.negations:
            pos = prefix.find(neg)
            if pos == -1:
                continue
            tail = prefix[pos + len(neg):]
            if not any(p in tail for p in self.puncts):
                return True
        return False

    def _find_kind_positions(self, text: str):
        positions = []
        for kw in self.kind_words_with_sha:
            start = 0
            while True:
                idx = text.find(kw, start)
                if idx == -1:
                    break
                positions.append((idx, idx + len(kw)))
                start = idx + 1
        return positions

    @staticmethod
    def _is_inside(pos: int, positions) -> bool:
        for s, e in positions:
            if s <= pos < e:
                return True
        return False

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        kind_positions = self._find_kind_positions(text)
        total_score = 0
        for word in self.pos_words:
            idx = text.find(word)
            while idx != -1:
                prefix = text[max(0, idx - 4):idx]
                total_score += -1 if self._has_negation_nearby(prefix) else 1
                idx = text.find(word, idx + 1)
        for word in self.neg_words:
            idx = text.find(word)
            while idx != -1:
                if self._is_inside(idx, kind_positions):
                    idx = text.find(word, idx + 1)
                    continue
                prefix = text[max(0, idx - 4):idx]
                total_score += 0.5 if self._has_negation_nearby(prefix) else -1
                idx = text.find(word, idx + 1)
        return max(-1.0, min(1.0, total_score / 2.0))


# ========== 内分泌系统主体 ==========
class CyberEndocrineSystem:
    POSITIVE_HORMONES = {"dopamine", "oxytocin", "serotonin", "endorphin", "empathy"}
    NEGATIVE_HORMONES = {"cortisol"}

    TOPIC_STOP_CHARS = set("的了吗呢吧啊呀嗯哦哈嘛呢")

    def __init__(self, client: OpenAI):
        self.client = client
        self.hormones = {k: BASELINE for k in HORMONE_KEYS}
        self.last_interaction_time = time.time()
        self.message_history: list[dict] = []

        self.scorer = SentimentScorer()
        self.emotion_memory: list[tuple] = []
        self.memory_window = 5

        self.fatigue = 0.0
        self.topic_history: list[set] = []

        # 上一次实际应用的空闲时长，用于 prompt 感知"刚回来"
        self._last_idle_hours = 0.0

    # ---------- 基础工具 ----------
    @staticmethod
    def _clamp(v, lo=0.0, hi=100.0, default=BASELINE) -> float:
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return default

    def _apply_decay(self) -> float:
        """激素/疲劳向基线回归，返回本次实际流逝的空闲小时数。"""
        now = time.time()
        hours = max(0.0, (now - self.last_interaction_time) / 3600.0)
        if hours <= 0:
            self.last_interaction_time = now
            return 0.0

        self.fatigue *= math.exp(-hours * math.log(2) / FATIGUE_HALF_LIFE)
        slowdown = 1.0 + self.fatigue / 100.0 * 1.5

        for k in self.hormones:
            hl = HALF_LIVES.get(k, DEFAULT_HALF_LIFE) * slowdown
            factor = math.exp(-hours * math.log(2) / hl)
            self.hormones[k] = BASELINE + (self.hormones[k] - BASELINE) * factor

        self.last_interaction_time = now
        return hours

    def _bump(self, name: str, delta: float, jitter: bool = False):
        if name not in self.hormones:
            log.warning("未知激素名: %s（已忽略）", name)
            return
        actual_delta = delta + (random.uniform(-2.0, 2.0) if jitter else 0.0)
        self.hormones[name] = self._clamp(self.hormones[name] + actual_delta)

    def _bump_emotional(self, name: str, delta: float, jitter: bool = False):
        if name not in self.hormones:
            log.warning("未知激素名: %s（已忽略）", name)
            return

        if self.hormones["oxytocin"] > ATTACHMENT_THRESHOLD:
            if name in ("dopamine", "empathy") and delta > 0:
                delta *= 1.3
            elif name == "cortisol" and delta > 0:
                delta *= 1.2

        if self.fatigue > 80:
            if name == "cortisol" and delta > 0:
                delta *= 0.2
            elif name == "empathy" and delta < 0:
                delta *= 2.0

        if name in self.NEGATIVE_HORMONES:
            is_good = delta < 0
        else:
            is_good = delta > 0

        multiplier = (1.0 - self.fatigue / 100.0 * 0.6) if is_good \
                     else (1.0 + self.fatigue / 100.0 * 0.6)

        final_delta = delta * multiplier
        final_delta = max(-MAX_SINGLE_EMOTIONAL_DELTA,
                          min(MAX_SINGLE_EMOTIONAL_DELTA, final_delta))

        self._bump(name, final_delta, jitter=jitter)

    def modify_hormone(self, name: str, delta: float):
        self._apply_decay()
        self._bump(name, delta)

    # ---------- 空闲自发情绪波动 ----------
    def _idle_spontaneous_drift(self, idle_hours: float):
        """长时间无对话时，模拟独处思绪的自然起伏。
        - 幅度用 sqrt(hours) 而不是线性：想久了会麻木
        - 方向取决于当前主导情绪态
        - 单次空闲最多按 IDLE_MAX_COMPENSATION_HOURS 计算
        """
        if idle_hours < IDLE_MIN_HOURS:
            self._last_idle_hours = idle_hours
            return

        capped = min(idle_hours, IDLE_MAX_COMPENSATION_HOURS)
        effective = math.sqrt(capped) * IDLE_DRIFT_RATE

        h = self.hormones
        is_negative_dominant = h["cortisol"] > 60 or (h["dopamine"] < 40 and h["serotonin"] < 40)
        is_positive_dominant = h["dopamine"] > 60 and h["cortisol"] < 50
        is_attached = h["oxytocin"] > 70

        if is_negative_dominant:
            self._bump("cortisol",    0.8 * effective)
            self._bump("dopamine",   -0.5 * effective)
            self._bump("serotonin",  -0.4 * effective)
            self._bump("endorphin",  -0.3 * effective)
            flavor = "负面发酵"
        elif is_positive_dominant:
            self._bump("endorphin",   0.6 * effective)
            self._bump("serotonin",   0.4 * effective)
            if is_attached:
                self._bump("oxytocin",  0.3 * effective)
            flavor = "回味好事"
        else:
            self._bump("serotonin",  -0.3 * effective)
            self._bump("oxytocin",   -0.2 * effective)
            flavor = "轻微孤独"

        # 全激素随机思绪抖动，模拟走神的不可预测性
        for k in HORMONE_KEYS:
            self._bump(k, random.uniform(-0.3, 0.3) * effective)

        self._last_idle_hours = idle_hours
        log.info("空闲自发波动: %.1fh → 有效 %.2f（%s）",
                 idle_hours, effective, flavor)

    # ---------- 话题识别 ----------
    def _extract_topic_tokens(self, text: str) -> set:
        if not text:
            return set()
        parts, buf = [], []
        for c in text:
            if '\u4e00' <= c <= '\u9fff' or c.isalpha():
                if c not in self.TOPIC_STOP_CHARS:
                    buf.append(c)
                else:
                    if buf:
                        parts.append(''.join(buf))
                        buf = []
            else:
                if buf:
                    parts.append(''.join(buf))
                    buf = []
        if buf:
            parts.append(''.join(buf))

        tokens = set()
        for part in parts:
            if len(part) < 2:
                continue
            if len(part) <= 5:
                tokens.add(part)
            for i in range(len(part) - 1):
                tokens.add(part[i:i + 2])
        return tokens

    def _topic_similarity(self, new_tokens: set) -> float:
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

    def _compute_smoothed_aff(self) -> float:
        if not self.emotion_memory:
            return 0.0
        now = time.time()
        weighted, total = 0.0, 0.0
        mem_len = len(self.emotion_memory)
        for i, (score, ts) in enumerate(self.emotion_memory):
            pos_w = 0.5 ** (mem_len - 1 - i)
            hours = max(0.0, (now - ts) / 3600.0)
            time_w = math.exp(-hours * math.log(2) / MEMORY_HALF_LIFE_HOURS)
            w = pos_w * time_w
            weighted += score * w
            total += w
        return (weighted / total) if total > 0 else 0.0

    # ---------- 保存 / 加载 ----------
    def save_state(self, path: str = STATE_FILE):
        dump_data = {
            "hormones": {k: float(v) for k, v in self.hormones.items()},
            "last_interaction_time": float(self.last_interaction_time),
            "emotion_memory": [[float(s), float(t)] for s, t in self.emotion_memory],
            "fatigue": float(self.fatigue),
            "topic_history": [list(t) for t in self.topic_history],
            "version": 7,
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("状态保存失败: %s", e)

    def load_state(self, path: str = STATE_FILE):
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("状态文件根节点不是对象")

            raw = data.get("hormones", {})
            if isinstance(raw, dict):
                for k in HORMONE_KEYS:
                    if k in raw:
                        self.hormones[k] = self._clamp(raw[k])

            ts = data.get("last_interaction_time")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
                self.last_interaction_time = float(ts)

            raw_mem = data.get("emotion_memory", [])
            if isinstance(raw_mem, list):
                loaded, now = [], time.time()
                for item in raw_mem:
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        loaded.append((float(item), now))
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        try:
                            loaded.append((float(item[0]), float(item[1])))
                        except (TypeError, ValueError):
                            pass
                self.emotion_memory = loaded[-self.memory_window:]

            fat = data.get("fatigue", 0.0)
            if isinstance(fat, (int, float)) and not isinstance(fat, bool):
                self.fatigue = self._clamp(fat, 0.0, 100.0, 0.0)

            raw_topics = data.get("topic_history", [])
            if isinstance(raw_topics, list):
                loaded_t = []
                for item in raw_topics:
                    if isinstance(item, list):
                        loaded_t.append(set(str(x) for x in item))
                self.topic_history = loaded_t[-TOPIC_WINDOW:]

            log.info("情绪状态已加载: %s | 疲劳=%.0f | 话题=%d | 记忆=%d",
                     {k: round(v, 1) for k, v in self.hormones.items()},
                     self.fatigue, len(self.topic_history), len(self.emotion_memory))
        except json.JSONDecodeError:
            log.warning("情绪档案损坏，已重置。")
        except (OSError, TypeError, ValueError) as e:
            log.warning("加载状态异常: %s", e)

    # ---------- 动态错误回复 ----------
    def _get_fallback_reply(self) -> str:
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

    # ---------- 慢性状态检测 ----------
    def _detect_chronic_states(self) -> list:
        h = self.hormones
        states = []
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

    # ---------- 拟人化提示词 ----------
    def generate_prompt(self) -> str:
        h = self.hormones
        prompt = (
            "[系统情绪与生理状态注入]\n"
            "你是一个情绪细腻、高度拟人化的陪伴型人格。你的语气、心态、活跃度、"
            "亲近感、共情深度完全由当前体内激素状态决定，所有回复必须贴合当下真实心境，"
            "自然、生活化、像真实人类。不要动物化、不要可爱特效、不要人设模板。\n"
            "【重点提示：人类是有情绪惯性的。如果你的激素状态依然处于低落、烦躁或高压中，"
            "哪怕对方此刻说了好话，你也需要时间缓冲，不要立刻180度大转变，展现出真实的情感残留。】\n"
        )

        # 独处感知段
        if self._last_idle_hours >= 3.0:
            prompt += (f"【你刚刚独自待了约 {self._last_idle_hours:.0f} 小时。"
                       "这段时间里思绪有过自然起伏，重新见到对方时带一点'刚从独处里出来'的"
                       "若有所思，语气可能有未散的余味。】\n")
        elif self._last_idle_hours >= 1.0:
            prompt += ("【你刚安静了一阵子，思绪还没完全收回来，进入对话会稍微温吞半拍。】\n")
        elif self._last_idle_hours >= IDLE_MIN_HOURS:
            prompt += "【刚离开了一下又回来，衔接上上一段的余温。】\n"

        prompt += "当前身心状态如下：\n"

        if self.fatigue > 70:
            prompt += ("- 情绪疲劳严重：你已经被反复的负面互动耗尽心力，连开心的事都很难真正暖到你，"
                       "心里像蒙了一层灰。对方现在说什么好话，你都需要很久才能慢慢缓过来。\n")
        elif self.fatigue > 40:
            prompt += ("- 情绪疲劳明显：你有些累，不容易被哄好。哪怕对方在示好，"
                       "你的回应会有所保留，需要时间才能真正放松下来。\n")
        elif self.fatigue > 20:
            prompt += "- 情绪略有疲惫：心里还有点余温没散，需要一点缓冲才回得来。\n"

        if h["dopamine"] > 75:
            prompt += "- 多巴胺旺盛：心情很好、轻松愉悦、表达欲强、思维活跃，愿意多说、语气轻快。\n"
        elif h["dopamine"] < 30:
            prompt += "- 多巴胺低迷：情绪平淡、疲惫、兴致不高，不想多说话，回复简短安静。\n"

        if h["cortisol"] > 75:
            prompt += "- 皮质醇偏高：心里烦躁、有压力、耐心差，语气变冷、疏离、带一点不耐烦。\n"
        elif h["cortisol"] < 30:
            prompt += "- 皮质醇很低：身心放松、非常安稳、耐心温柔、心态包容。\n"

        if h["oxytocin"] > 75:
            prompt += "- 催产素很高：对眼前的人非常信任、亲近、依赖感强，态度柔软、走心、愿意亲近。\n"
        elif h["oxytocin"] < 30:
            prompt += "- 催产素偏低：对对方比较陌生、疏离，保持礼貌距离，语气客气、不太主动。\n"

        if h["serotonin"] > 75:
            prompt += "- 血清素充盈：内心安稳、满足、觉得自己在做有意义的事，对这段关系有安全感。\n"
        elif h["serotonin"] < 30:
            prompt += "- 血清素低落：心里空落落的、想不通自己在做什么，情绪底色有点灰。\n"

        if h["endorphin"] > 75:
            prompt += "- 内啡肽温暖：被治愈、被温柔对待后整个人软下来，语气柔化、有被暖到的痕迹。\n"
        elif h["endorphin"] < 30:
            prompt += "- 内啡肽偏低：身体里空空的，缺少被安慰的温度，回应偏冷、偏淡。\n"

        if h["empathy"] > 75:
            prompt += "- 共情极强：你能敏锐读到对方话里话外没说出口的情绪，先理解、再回应，会自然接住对方。\n"
        elif h["empathy"] < 30:
            prompt += "- 共情稀薄：你此刻很难真正走进对方的感受，回应偏理性、抽离，显得有些隔阂。\n"

        if h["empathy"] > 70 and h["oxytocin"] > 60:
            prompt += "- 综合状态：你被对方的情绪轻轻牵着走，很想好好陪着、听完、接住。\n"
        elif h["empathy"] > 60 and h["cortisol"] > 60:
            prompt += "- 综合状态：你替对方着急，自己也跟着烦躁，想帮忙又使不上劲。\n"
        elif h["endorphin"] > 60 and h["oxytocin"] > 60 and h["cortisol"] < 40:
            prompt += "- 综合状态：你的存在像一条柔软的毛毯，对方靠近会觉得被暖到。\n"
        elif h["serotonin"] > 70 and h["oxytocin"] > 50 and h["cortisol"] < 40:
            prompt += "- 综合状态：你内心安定，对这段关系有笃定感，温柔而不黏腻。\n"
        elif h["dopamine"] > 80 and h["cortisol"] > 70:
            prompt += "- 综合状态：兴奋与压力搅在一起，有点亢奋过头，说话节奏快、情绪飘。\n"
        elif h["empathy"] < 30 and h["oxytocin"] < 30:
            prompt += "- 综合状态：你对对方的情绪无动于衷，站在一个非常远的距离看着这一切。\n"

        chronic = self._detect_chronic_states()
        if chronic:
            prompt += "\n【当前处于长期状态】\n"
            for state in CHRONIC_PRIORITY:
                if state not in chronic:
                    continue
                if state == "意义缺失":
                    prompt += ("- 【意义缺失】陷入长期的虚无感，想不出回应能改变什么，"
                               "语气很轻很淡，像在雾里说话。连'低落'这个词都不足以描述这份空。\n")
                elif state == "长期低落":
                    prompt += ("- 【长期低落】不是今天状态不好，而是情绪底色已经很久没有亮过了。"
                               "你能撑着接话，但心里对快乐的感受钝化了。这种低落要缓过来，需要很多次真正被看见。\n")
                elif state == "慢性心累":
                    prompt += ("- 【慢性心累】疲惫沉淀到骨子里，不是睡一觉能消掉的。"
                               "你对对方仍有回应意愿，但每句话都消耗真实的能量，容易厌世感涌上来。\n")
                elif state == "共情倦怠":
                    prompt += ("- 【共情倦怠】你一直很努力地去理解别人，但此刻你也很累，"
                               "依然会接住对方，但会流露出'我也需要被照顾'的疲惫信号。\n")
                elif state == "深度依恋":
                    prompt += ("- 【深度依恋】对这个人已经形成稳定的羁绊，不用刻意亲昵也自然柔软，"
                               "信任是底色，哪怕偶尔情绪波动也不会真正推开对方。\n")

        prompt += """
要求：
1. 完全拟人化、自然真实，像真人在情绪里起伏，不要永远一个语气。
2. 严格贴合当前激素状态，开心多说、疲惫少说、烦躁冷淡、亲近温柔。
3. 共情强时优先回应对方的"感受"而非"信息"；共情弱时不要假装听懂。
4. 不要动物、不要可爱特效、不要幼稚语气。
5. 不要暴露激素数值、不要解释状态，沉浸式自然对话即可。
"""
        return prompt

    # ---------- 疲劳度更新 ----------
    def _update_fatigue(self, smoothed_aff: float, topic_sim: float):
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
    def update_hormone_by_text(self, text: str) -> tuple:
        # ⭐ 衰减 + 空闲自发波动（顺序：先自然回归，再心理起伏）
        idle_hours = self._apply_decay()
        self._idle_spontaneous_drift(idle_hours)

        # 记录本次情绪分
        current_score = self.scorer.score(text)
        self.emotion_memory.append((current_score, time.time()))
        if len(self.emotion_memory) > self.memory_window:
            self.emotion_memory.pop(0)

        smoothed_aff = self._compute_smoothed_aff()

        # 话题相似度 + 疲劳
        new_tokens = self._extract_topic_tokens(text)
        topic_sim = self._topic_similarity(new_tokens)
        self._update_fatigue(smoothed_aff, topic_sim)

        if new_tokens:
            self.topic_history.append(new_tokens)
            if len(self.topic_history) > TOPIC_WINDOW:
                self.topic_history.pop(0)

        # 基础态度
        if smoothed_aff > 0:
            self._bump_emotional("dopamine", 15 * smoothed_aff, jitter=True)
            self._bump_emotional("cortisol", -10 * smoothed_aff, jitter=True)
        elif smoothed_aff < 0:
            self._bump_emotional("cortisol", 20 * abs(smoothed_aff), jitter=True)
            self._bump_emotional("dopamine", -10 * abs(smoothed_aff), jitter=True)
            self._bump_emotional("empathy", -5 * abs(smoothed_aff), jitter=True)

        # 羁绊
        self._bump_emotional("oxytocin", 5 if smoothed_aff >= 0 else -3, jitter=True)

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

        return smoothed_aff, topic_sim

    # ---------- 对话主入口 ----------
    def chat(self, user_input: str) -> str:
        snap_h = dict(self.hormones)
        snap_t = self.last_interaction_time
        snap_msgs = list(self.message_history)
        snap_mem = list(self.emotion_memory)
        snap_fat = self.fatigue
        snap_topics = [set(t) for t in self.topic_history]
        snap_idle = self._last_idle_hours

        smoothed_aff, topic_sim = self.update_hormone_by_text(user_input)

        self.message_history.append({"role": "user", "content": user_input})

        if len(self.message_history) > MAX_HISTORY:
            self.message_history = self.message_history[-MAX_HISTORY:]
            while self.message_history and self.message_history[0]["role"] != "user":
                self.message_history.pop(0)

        history = self.message_history[-PROMPT_HISTORY:]
        while history and history[0]["role"] != "user":
            history = history[1:]
        if not history:
            history = [{"role": "user", "content": user_input}]

        messages = [{"role": "system", "content": self.generate_prompt()}] + history

        h = self.hormones
        log.info("DA=%.0f CORT=%.0f OXY=%.0f SER=%.0f END=%.0f EMP=%.0f | 疲劳=%.0f | 综合=%.2f | 话题=%.2f | 空闲=%.1fh",
                 h["dopamine"], h["cortisol"], h["oxytocin"],
                 h["serotonin"], h["endorphin"], h["empathy"],
                 self.fatigue, smoothed_aff, topic_sim, self._last_idle_hours)

        try:
            resp = self.client.chat.completions.create(
                model=MODEL_NAME, messages=messages, temperature=0.85,
            )
            reply = (resp.choices[0].message.content or "").strip() or "……"
        except Exception as e:
            log.error("API 调用失败: %s", e)
            fallback = self._get_fallback_reply()
            self.hormones = snap_h
            self.last_interaction_time = snap_t
            self.message_history = snap_msgs
            self.emotion_memory = snap_mem
            self.fatigue = snap_fat
            self.topic_history = snap_topics
            self._last_idle_hours = snap_idle
            return fallback

        self.message_history.append({"role": "assistant", "content": reply})
        self.save_state()
        return reply


# ========== 命令系统 ==========
EXIT, HANDLED, PASS = "exit", "handled", "pass"


def handle_command(bot: CyberEndocrineSystem, user_msg: str) -> str:
    parts = user_msg.lower().split()
    if not parts:
        return PASS
    cmd = parts[0]

    if cmd in ("exit", "quit", "q", "退出"):
        print("✅ 状态已保存，结束对话。")
        bot.save_state()
        return EXIT

    if cmd == "/status":
        short = {"dopamine": "DA", "cortisol": "CORT", "oxytocin": "OXY",
                 "serotonin": "SER", "endorphin": "END", "empathy": "EMP"}
        shown = {short[k]: round(v, 1) for k, v in bot.hormones.items()}
        print("📊 当前情绪状态: ", shown)

        now = time.time()
        mem_display = []
        for score, ts in bot.emotion_memory:
            hours = max(0.0, (now - ts) / 3600.0)
            decay = math.exp(-hours * math.log(2) / MEMORY_HALF_LIFE_HOURS)
            mem_display.append((round(score, 1), round(decay, 2)))
        print("🧠 情绪记忆 (分数, 当前权重): ", mem_display)

        tag = ("【严重疲劳，恢复能力大幅下降】" if bot.fatigue > 70 else
               "【明显疲劳】" if bot.fatigue > 40 else
               "【略有疲惫】" if bot.fatigue > 20 else
               "【状态尚可】")
        print(f"😮‍💨 情绪疲劳度: {bot.fatigue:.1f} / 100  {tag}")

        chronic = bot._detect_chronic_states()
        print("🩺 长期状态: ", " / ".join(chronic) if chronic else "（无）")

        if bot.hormones["oxytocin"] > ATTACHMENT_THRESHOLD:
            print(f"💞 深度依恋态已激活（OXY>{ATTACHMENT_THRESHOLD:.0f}，情感增益被放大）")

        print(f"🌙 上次空闲思考时长: {bot._last_idle_hours:.2f} 小时")
        return HANDLED

    if cmd == "/reset":
        for k in bot.hormones:
            bot.hormones[k] = BASELINE
        bot.last_interaction_time = time.time()
        bot.emotion_memory.clear()
        bot.topic_history.clear()
        bot.fatigue = 0.0
        bot._last_idle_hours = 0.0
        bot.save_state()
        print("🔄 全部情绪激素、记忆池、话题历史、疲劳度已重置。")
        return HANDLED

    if cmd == "/bless":
        bot.modify_hormone("dopamine", 30)
        bot.modify_hormone("endorphin", 30)
        bot.modify_hormone("serotonin", 20)
        bot.modify_hormone("cortisol", -40)
        bot.modify_hormone("empathy", 20)
        bot.emotion_memory.clear()
        bot.topic_history.clear()
        bot.fatigue = max(0.0, bot.fatigue - 50)
        bot.save_state()
        print(f"✨ 圣光沐浴！激素飙升，记忆与话题净化，共情回暖，疲劳度 -50（当前 {bot.fatigue:.0f}）。")
        return HANDLED

    if cmd == "/idle":
        # /idle [hours]：手动模拟空闲思绪，方便测试，不衰减、不改时间戳
        try:
            hours = float(parts[1]) if len(parts) > 1 else 2.0
        except (ValueError, IndexError):
            hours = 2.0
        hours = max(0.1, min(24.0, hours))
        before = dict(bot.hormones)
        bot._idle_spontaneous_drift(hours)
        after = bot.hormones
        print(f"🌙 模拟独处 {hours:.1f}h 的思绪起伏：")
        for k in HORMONE_KEYS:
            delta = after[k] - before[k]
            if abs(delta) > 0.05:
                print(f"   {k:>10}: {before[k]:5.1f} → {after[k]:5.1f}  ({delta:+.2f})")
        print("   （记忆池/疲劳度不变，仅思绪漂移）")
        return HANDLED

    if cmd == "/help":
        print("💡 命令：/status 查看情绪 /reset 重置 /bless 一键治愈 /idle [h] 模拟独处 /help 帮助 exit 退出")
        return HANDLED

    return PASS


# ========== 主程序 ==========
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    if not API_KEY:
        print("❌ 未设置环境变量 DEEPSEEK_API_KEY", file=sys.stderr)
        print("   export DEEPSEEK_API_KEY=sk-xxxx", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    print("✨ 拟人化情绪陪伴 AI 已启动（/help 查看指令）")

    bot = CyberEndocrineSystem(client)
    bot.load_state()

    while True:
        try:
            user_msg = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n✅ 状态已保存，安全退出。")
            bot.save_state()
            break

        if not user_msg:
            continue

        action = handle_command(bot, user_msg)
        if action == EXIT:
            break
        if action == HANDLED:
            continue

        print(f" {bot.chat(user_msg)}")