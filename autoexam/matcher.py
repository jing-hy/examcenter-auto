# -*- coding: utf-8 -*-
"""题干归一化、错题本匹配、本地答案缓存。

为什么需要归一化：错题本接口返回的 stem 与作答页拿到的 stem 都来自同一份
题库，但顺序/空白/图片标记在不同入口可能有细微差别（前端还会把图片 md 渲染成
"[图]" 或 <img>）。所以比较前统一：图片占位、全角转半角、去掉空白与标点。

匹配键 = (套卷名归一化, 题干归一化后的 sha1)。套卷名参与匹配，正是为了满足
"区分不同套题"的要求：同题干出现在两套卷里也不会串答案。
"""

import hashlib
import io
import json
import os
import re
import time
import unicodedata

IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HTML_IMG_RE = re.compile(r"<img[^>]*>", re.I)


def norm_text(text):
    """归一化：图片占位、全角→半角、去空白/标点/控制字符、转小写。"""
    value = str(text or "")
    value = IMG_RE.sub("图", value)
    value = HTML_IMG_RE.sub("图", value)
    value = unicodedata.normalize("NFKC", value)
    out = []
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] in ("Z", "P", "C"):
            continue
        out.append(ch.lower())
    return "".join(out)


def stem_hash(stem):
    """题干归一化后的短哈希。"""
    return hashlib.sha1(norm_text(stem).encode("utf-8")).hexdigest()[:16]


def paper_key(name):
    return norm_text(name)


def stem_preview(stem, width=28):
    text = " ".join(str(stem or "").split())
    text = IMG_RE.sub("[图]", text)
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


class WrongBookIndex(object):
    """把 /api/wrong 的条目建成可查索引。"""

    def __init__(self, items):
        self.items = list(items or [])
        self.by_hash = {}
        self.by_paper = {}
        for item in self.items:
            pkey = paper_key(item.get("paper_name"))
            norm = norm_text(item.get("stem"))
            if not pkey or not norm:
                continue
            self.by_hash.setdefault((pkey, stem_hash(item.get("stem"))), []).append(item)
            self.by_paper.setdefault(pkey, []).append((item, norm))

    def __len__(self):
        return len(self.items)

    def find(self, paper_name, stem):
        """返回 {'status','answer','item','via'}；status ∈ found/ambiguous/missing。"""
        pkey = paper_key(paper_name)
        norm = norm_text(stem)
        if not pkey or not norm:
            return {"status": "missing", "answer": "", "item": None, "via": ""}

        candidates = self.by_hash.get((pkey, stem_hash(stem))) or []
        result = self._resolve(candidates, "exact")
        if result["status"] != "missing":
            return result

        # 兜底：同一套卷里找"短的一方是长的一方的前缀"的条目
        # （两端文本首尾被截断时用得上；错题本条数很少，线扫描不心疼）
        loose = []
        for item, item_norm in self.by_paper.get(pkey, []):
            size = min(len(item_norm), len(norm))
            if size >= 12 and item_norm[:size] == norm[:size]:
                loose.append(item)
        result = self._resolve(loose, "prefix")
        if result["status"] != "missing":
            return result

        return {"status": "missing", "answer": "", "item": None, "via": ""}

    def _resolve(self, candidates, via):
        if not candidates:
            return {"status": "missing", "answer": "", "item": None, "via": ""}
        answers = {}
        for item in candidates:
            answer = str(item.get("answer") or "").strip()
            if not answer:
                continue
            answers.setdefault(answer, item)
        if not answers:
            return {"status": "missing", "answer": "", "item": None, "via": ""}
        if len(answers) > 1:
            return {
                "status": "ambiguous",
                "answer": "",
                "item": None,
                "via": via,
                "answers": sorted(answers.keys()),
            }
        answer = list(answers.keys())[0]
        return {"status": "found", "answer": answer, "item": answers[answer], "via": via}


def answer_is_sane(question, answer):
    """粗筛：答案形态与题型是否对得上（防跨题误配）。"""
    answer = str(answer or "").strip()
    if not answer:
        return False
    qtype = question.get("type")
    if qtype in ("single", "judge", "multi"):
        keys = [str(o.get("key") or "").strip().upper()
                for o in question.get("options") or [] if o.get("key")]
        if not keys:
            return True
        letters = [ch for ch in answer.upper() if ch.isalpha()]
        if not letters:
            return False
        return all(ch in keys for ch in letters)
    return True


class AnswerCache(object):
    """跨账号/跨运行复用的答案缓存（JSON 文件，读-改-写 + 原子替换）。"""

    VERSION = 1

    def __init__(self, path, enabled=True, logger=None):
        self.path = path
        self.enabled = bool(enabled)
        self.logger = logger
        self.entries = {}
        self.hits = 0
        if self.enabled and path and os.path.isfile(path):
            self._load()

    def _load(self):
        try:
            with io.open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                self.entries = entries
        except Exception:
            self.entries = {}

    def _reload(self):
        """多进程共用同一文件时，写之前先合并别人的新增。"""
        try:
            with io.open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                for key, value in entries.items():
                    self.entries.setdefault(key, value)
        except Exception:
            pass

    @staticmethod
    def _key(paper_id, stem):
        return "%s|%s" % (int(paper_id), stem_hash(stem))

    def get(self, paper_id, stem):
        if not self.enabled:
            return None
        entry = self.entries.get(self._key(paper_id, stem))
        if isinstance(entry, dict) and str(entry.get("answer") or "").strip():
            self.hits += 1
            return entry
        return None

    def put(self, paper_id, paper_name, stem, answer, source):
        if not self.enabled:
            return False
        answer = str(answer or "").strip()
        if not answer:
            return False
        key = self._key(paper_id, stem)
        entry = {
            "answer": answer,
            "source": source,
            "paper_name": paper_name,
            "stem": stem_preview(stem),
            "ts": int(time.time()),
        }
        if self.entries.get(key) == entry:
            return False
        self.entries[key] = entry
        self.save()
        return True

    def save(self):
        if not self.enabled or not self.path:
            return
        self._reload()
        payload = {"version": self.VERSION, "entries": self.entries}
        folder = os.path.dirname(os.path.abspath(self.path))
        if folder and not os.path.isdir(folder):
            try:
                os.makedirs(folder)
            except OSError:
                pass
        tmp = self.path + ".tmp"
        try:
            with io.open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=1))
            if os.path.exists(self.path):
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            os.replace(tmp, self.path)
        except Exception as exc:
            if self.logger:
                self.logger.warn("写答案缓存失败：%s" % exc)

    def size(self):
        return len(self.entries)
