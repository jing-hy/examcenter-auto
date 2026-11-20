# -*- coding: utf-8 -*-
"""考试中心接口封装。

路径前缀来自前端 core.js:36 的 window.EXAM_BASE = '/plugin/examcenter'。
字段名严格按 student.js 里实际读取的键名（契约见 README 的接口说明）。
"""

from . import netio

BASE = "/plugin/examcenter"


class ExamCenter(object):
    def __init__(self, client, logger=None):
        self.client = client
        self.logger = logger

    # ------------------------------------------------------------------ 基础
    def _req(self, path, method="GET", body=None, tag=None):
        return self.client.json_request(BASE + path, method=method, body=body, tag=tag)

    def me(self):
        data = self._req("/api/me")
        return (data or {}).get("user") or {}

    # ------------------------------------------------------------------ 试卷
    def papers(self):
        data = self._req("/api/papers", tag="papers")
        items = (data or {}).get("items")
        return items if isinstance(items, list) else []

    def find_paper(self, paper_id):
        try:
            wanted = int(paper_id)
        except (TypeError, ValueError):
            return None
        for paper in self.papers():
            if int(paper.get("id") or 0) == wanted:
                return paper
        return None

    # ------------------------------------------------------------------ 作答
    def start_attempt(self, paper_id, mode="exam", fresh=True, points_opt_in=None):
        body = {
            "paper_id": int(paper_id),
            "mode": mode,
            "fresh": bool(fresh),
        }
        if points_opt_in is not None:
            body["points_opt_in"] = bool(points_opt_in)
        return self._req("/api/attempts", method="POST", body=body, tag="start-attempt")

    def answer(self, attempt_id, question_id, value):
        return self._req(
            "/api/attempts/%d/answer" % int(attempt_id),
            method="POST",
            body={"question_id": int(question_id), "answer": value},
            tag="answer",
        )

    def beat(self, attempt_id, index):
        return self._req(
            "/api/attempts/%d/beat" % int(attempt_id),
            method="POST",
            body={"index": int(index)},
            tag="beat",
        )

    def submit(self, attempt_id):
        return self._req("/api/attempts/%d/submit" % int(attempt_id),
                         method="POST", body=None, tag="submit")

    def review(self, attempt_id):
        return self._req("/api/attempts/%d" % int(attempt_id), tag="review")

    def history(self, limit=50):
        data = self._req("/api/attempts?limit=%d" % int(limit), tag="history")
        items = (data or {}).get("items")
        return items if isinstance(items, list) else []

    # ---------------------------------------------------------------- 错题本
    # 实测（2026-11-20，真实服务器）：/api/wrong 支持 limit/offset 分页，
    # 默认返回全量但页大小未公开。所以这里显式分页，避免条目变多后被悄悄截断。
    WRONG_PAGE_SIZE = 200

    def wrong(self, page_size=None, max_pages=50):
        size = int(page_size or self.WRONG_PAGE_SIZE)
        if size <= 0:
            size = self.WRONG_PAGE_SIZE
        items = []
        seen = set()
        offset = 0
        for _ in range(max(1, int(max_pages or 1))):
            data = self._req("/api/wrong?limit=%d&offset=%d" % (size, offset), tag="wrong")
            batch = (data or {}).get("items")
            if not isinstance(batch, list) or not batch:
                break
            fresh = 0
            for item in batch:
                key = item.get("id")
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                items.append(item)
                fresh += 1
            # 这一页不满 = 到底了；这一页全是旧条目 = 服务端不认 offset，别再转
            if len(batch) < size or fresh == 0:
                break
            offset += len(batch)
        return items

    # ------------------------------------------------------------------ 其它
    def stats_me(self):
        return self._req("/api/stats/me", tag="stats")

    def logout(self):
        try:
            return self._req("/api/logout", method="POST", tag="logout")
        except netio.ApiError:
            return None

    def change_password(self, old_password, new_password):
        return self._req("/api/me/password", method="POST",
                         body={"old_password": old_password, "new_password": new_password},
                         tag="password")


# ------------------------------------------------------------------ 纯函数工具
def flatten_questions(questions):
    """把接口返回的题目摊平成 slots（综合题展开 sub_questions）。

    与前端 student.js:199-203 的口径一致：综合题的每个子题各占一题。
    """
    slots = []
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        subs = question.get("sub_questions") or []
        if question.get("type") == "comprehensive" and subs:
            for sub in subs:
                slots.append({"q": sub, "group": question})
        else:
            slots.append({"q": question, "group": None})
    return slots


def option_keys(question):
    keys = []
    for option in question.get("options") or []:
        key = str(option.get("key") or "").strip()
        if key:
            keys.append(key)
    return keys


def question_label(question):
    return {
        "single": "单选题",
        "multi": "多选题",
        "judge": "判断题",
        "fill": "填空题",
        "comprehensive": "综合题",
    }.get(question.get("type"), str(question.get("type") or "未知题"))


POINTS_LIMIT = 3


def points_remaining(paper):
    """这套卷还能计几次积分；没有积分功能 / 次数用满时返回 0。"""
    paper = paper or {}
    if not paper.get("points_enabled") and not paper.get("formal_exam"):
        return 0
    try:
        awards = int(paper.get("points_awards") or 0)
    except (TypeError, ValueError):
        awards = 0
    return max(0, POINTS_LIMIT - awards)


def paper_line(paper, index=None):
    """试卷列表里的一行文字。"""
    parts = []
    if index is not None:
        parts.append("%2d." % index)
    parts.append(str(paper.get("name") or "未命名"))
    parts.append("id=%s" % paper.get("id"))
    parts.append("%s题" % paper.get("total"))
    parts.append("满分%s" % _trim(paper.get("max_score")))
    if paper.get("shuffle"):
        parts.append("题目乱序")
    if paper.get("formal_exam"):
        parts.append("正式考试卷")
    if paper.get("exam_open") in (0, False):
        parts.append("模考未开放")
    if paper.get("practice_open") in (0, False):
        parts.append("练习未开放")
    if paper.get("points_enabled"):
        awards = int(paper.get("points_awards") or 0)
        parts.append("积分%d/%d" % (awards, POINTS_LIMIT))
        left = points_remaining(paper)
        parts.append("还可计分%d次" % left if left else "积分已满")
    doing = paper.get("doing")
    if isinstance(doing, dict):
        parts.append("有未完成进度 %s/%s题" % (doing.get("answered") or 0, doing.get("total") or paper.get("total")))
    return "  ".join(parts)


def _trim(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "0")
    if abs(number - round(number)) < 0.01:
        return str(int(round(number)))
    return ("%.1f" % number)
