"""中文情感打分器（词典法）。

从 hormone_reference.py 原样移植：正/负词表 + 否定词翻转 +
"傻瓜"类亲昵词豁免，输出综合分 [-1, 1]。
"""

from __future__ import annotations


class SentimentScorer:
    """基于中文词典的轻量情感打分器。"""

    def __init__(self) -> None:
        """初始化正向/负向词表、否定词与亲昵豁免词。"""
        self.pos_words = {"棒", "可爱", "喜欢", "厉害", "好耶", "不错",
                          "爱你", "么么哒", "谢谢", "优秀", "牛",
                          "一定行", "可以的", "稳了"}
        self.neg_words = {"笨", "垃圾", "烦", "讨厌", "滚", "无语",
                          "闭嘴", "傻", "差劲", "烂", "恶心"}
        self.negations = {"不", "没", "别", "无", "非"}
        self.kind_words_with_sha = ("傻瓜", "傻乎乎", "傻笑", "小傻")
        self.puncts = set("，。！？、；：,.!?;:…~ \t\n")

    def _has_negation_nearby(self, prefix: str) -> bool:
        """词前 4 字内是否出现否定词（中间不隔标点）。"""
        for neg in self.negations:
            pos = prefix.find(neg)
            if pos == -1:
                continue
            tail = prefix[pos + len(neg):]
            if not any(p in tail for p in self.puncts):
                return True
        return False

    def _find_kind_positions(self, text: str) -> list[tuple[int, int]]:
        """找出所有亲昵"傻"类词的位置区间（这些位置内的"傻"按正面豁免）。"""
        positions: list[tuple[int, int]] = []
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
    def _is_inside(pos: int, positions: list[tuple[int, int]]) -> bool:
        """判断位置是否落在任一区间内。"""
        for s, e in positions:
            if s <= pos < e:
                return True
        return False

    def score(self, text: str) -> float:
        """对文本打分，返回 [-1.0, 1.0] 的综合情感分。"""
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
