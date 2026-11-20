# -*- coding: utf-8 -*-
"""本地契约服务器（离线靶场）。

它按「考试中心」前端 JS 逆向出来的接口契约实现服务端，用途有三：
1. 本机连不上真实服务器时，能端到端跑通并验证整个刷题流程；
2. 把契约固化成可执行的断言，后面接手的人改错字段名会立刻崩；
3. 用 --serve-ui 还能把保存下来的真实前端挂上去，肉眼对照。

它**不是**真实服务器的复刻：真实服务端行为里仍有未知项（见交接文档），
这里按前端代码表现出的最小合理语义实现。

用法：
    python mock/server.py --port 8420 --questions 12 --serve-ui
    python -m mock.server --port 8420
"""

import argparse
import copy
import io
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
import uuid

try:
    from . import seed as seedmod
except ImportError:  # 直接 python mock/server.py 运行时
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import seed as seedmod

BASE_PATH = "/plugin/examcenter"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_HTML_NAMES = [
    "考试中心 · 通用技术刷题.html",
    "考试中心 · 通用技术刷题1.html",
]
STATIC_DIR_NAME = "考试中心 · 通用技术刷题_files"


# --------------------------------------------------------------------------- 工具
def norm_choice(value):
    return "".join(sorted(set(str(value or "").strip().upper())))


def norm_fill(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if not ch.isspace())
    return text.lower()


def judge_answer(question, value):
    """按题型判分；返回 (is_correct, score)。"""
    qtype = question.get("type")
    correct = question.get("answer") or ""
    if qtype in ("single", "judge", "multi"):
        ok = bool(norm_choice(value)) and norm_choice(value) == norm_choice(correct)
    else:
        ok = bool(norm_fill(correct)) and norm_fill(value) == norm_fill(correct)
    return ok, (float(question.get("score") or 0) if ok else 0.0)


class Database(object):
    """进程内状态库（内存版），加锁供多线程访问。"""

    def __init__(self, papers, users, leak_answers=False, latency=0.0, verbose=False):
        self.lock = threading.RLock()
        self.papers = papers
        self.users = users
        self.sessions = {}
        self.attempts = {}
        self.finished = []
        self.answer_state = {}
        self.points = {}
        self.next_aid = [1001]
        self.leak_answers = bool(leak_answers)
        self.latency = float(latency or 0.0)
        self.verbose = bool(verbose)
        # qid -> (paper, leaf question)
        self.leaf_index = {}
        for paper in papers:
            for question in paper["questions"]:
                for leaf in seedmod.flatten(question):
                    self.leaf_index[leaf["id"]] = (paper, leaf)

    # ---------------------------------------------------------------- 会话
    def login(self, username, password):
        user = self.users.get(username)
        if not user or user["password"] != password:
            return None
        sid = uuid.uuid4().hex
        with self.lock:
            self.sessions[sid] = username
        return sid

    def user_of(self, sid):
        with self.lock:
            username = self.sessions.get(sid)
        if not username:
            return None
        return self.users.get(username)

    def logout(self, sid):
        with self.lock:
            self.sessions.pop(sid, None)

    # ---------------------------------------------------------------- 试卷
    def visible_papers(self, user):
        items = []
        for paper in self.papers:
            if paper.get("hidden"):
                continue
            latest = self.latest_attempt(user["username"], paper["id"], submitted=True)
            doing = self.latest_attempt(user["username"], paper["id"], submitted=False)
            answered = 0
            accuracy = 0.0
            if doing:
                answered = len(doing["answers"])
            elif latest:
                answered = len(latest["answers"])
            if latest and latest.get("total"):
                correct = sum(1 for a in latest["answers"].values() if a.get("is_correct"))
                accuracy = round(correct * 100.0 / latest["total"], 1)
            item = {
                "id": paper["id"],
                "name": paper["name"],
                "total": paper["total"],
                "max_score": paper["max_score"],
                "shuffle": paper.get("shuffle", False),
                "formal_exam": paper.get("formal_exam", False),
                "hidden": paper.get("hidden", False),
                "practice_open": paper.get("practice_open", 1),
                "exam_open": paper.get("exam_open", 1),
                "points_enabled": paper.get("points_enabled", False),
                "points_rules": paper.get("points_rules", []),
                "points_awards": self.points.get((user["username"], paper["id"]), 0),
                "answered": answered,
                "accuracy": accuracy,
            }
            if doing:
                item["doing"] = {
                    "mode": doing["mode"],
                    "answered": len(doing["answers"]),
                    "total": doing["total"],
                    "elapsed": int(time.time() - doing["started"]),
                    "points_opt_in": doing.get("points_opt_in", True),
                }
            items.append(item)
        return items

    def paper_of(self, paper_id):
        for paper in self.papers:
            if paper["id"] == int(paper_id):
                return paper
        return None

    def latest_attempt(self, username, paper_id, submitted=False):
        found = None
        for attempt in self.attempts.values():
            if attempt["username"] != username or attempt["paper_id"] != int(paper_id):
                continue
            if bool(attempt["submitted"]) != bool(submitted):
                continue
            if found is None or attempt["started"] >= found["started"]:
                found = attempt
        return found

    # ---------------------------------------------------------------- 作答
    def start_attempt(self, user, paper, mode, fresh, points_opt_in):
        with self.lock:
            existing = None
            for attempt in self.attempts.values():
                if attempt["submitted"]:
                    continue
                if attempt["username"] == user["username"] and attempt["paper_id"] == paper["id"] \
                        and attempt["mode"] == mode:
                    existing = attempt
                    break
            resumed = False
            if existing and not fresh:
                attempt = existing
                resumed = True
            else:
                if existing:
                    self.attempts.pop(existing["id"], None)
                attempt = self._new_attempt(user, paper, mode, points_opt_in)
            if points_opt_in is not None:
                attempt["points_opt_in"] = bool(points_opt_in)
            return attempt, resumed

    def _new_attempt(self, user, paper, mode, points_opt_in):
        units = list(paper["questions"])
        if paper.get("shuffle"):
            import random
            rng = random.Random(paper["id"] * 7 + 3)
            rng.shuffle(units)
        order = []
        for unit in units:
            for leaf in seedmod.flatten(unit):
                order.append(leaf["id"])
        attempt = {
            "id": self.next_aid[0],
            "username": user["username"],
            "paper_id": paper["id"],
            "mode": mode,
            "units": units,
            "order": order,
            "total": len(order),
            "answers": {},
            "started": time.time(),
            "cursor": 0,
            "submitted": False,
            "points_opt_in": bool(points_opt_in) if points_opt_in is not None else True,
        }
        self.next_aid[0] += 1
        self.attempts[attempt["id"]] = attempt
        return attempt

    def questions_payload(self, attempt):
        paper = self.paper_of(attempt["paper_id"])
        mode = attempt["mode"]
        items = []
        for unit in attempt["units"]:
            item = copy.deepcopy(unit)
            if mode == "exam" and not self.leak_answers:
                _strip_answer(item)
            items.append(item)
        return items

    def answer_question(self, attempt, question_id, value):
        paper, question = self.leaf_index.get(int(question_id), (None, None))
        if question is None:
            raise KeyError("题目不存在")
        if int(question_id) not in attempt["order"]:
            raise KeyError("题目不属于本次作答")
        ok, score = judge_answer(question, value)
        record = {
            "user_answer": value,
            "is_correct": ok,
            "score": score,
            "correct_answer": question.get("answer") or "",
            "explanation": question.get("explanation") or "",
        }
        with self.lock:
            attempt["answers"][int(question_id)] = record
            key = (attempt["username"], int(question_id))
            state = self.answer_state.get(key) or {"wrong_times": 0}
            state["is_correct"] = ok
            state["user_answer"] = value
            state["paper_id"] = paper["id"]
            if not ok:
                state["wrong_times"] = int(state.get("wrong_times") or 0) + 1
            self.answer_state[key] = state
        hide = attempt["mode"] == "exam" and not self.leak_answers
        if hide:
            return {
                "is_correct": None,
                "score": 0,
                "correct_answer": "",
                "explanation": "",
                "hidden": True,
            }
        return {
            "is_correct": ok,
            "score": score,
            "correct_answer": record["correct_answer"],
            "explanation": record["explanation"],
        }

    def wrong_list(self, user):
        with self.lock:
            rows = []
            for (username, qid), state in self.answer_state.items():
                if username != user["username"]:
                    continue
                if state.get("is_correct"):
                    continue
                paper, question = self.leaf_index.get(qid, (None, None))
                if question is None:
                    continue
                rows.append({
                    "id": qid,
                    "paper_id": paper["id"],
                    "paper_name": paper["name"],
                    "seq": question.get("seq") or 0,
                    "stem": question.get("stem") or "",
                    "knowledge": question.get("knowledge") or "",
                    "wrong_times": int(state.get("wrong_times") or 1),
                    "options": copy.deepcopy(question.get("options") or []),
                    "answer": question.get("answer") or "",
                    "user_answer": state.get("user_answer") or "",
                    "explanation": question.get("explanation") or "",
                    "images": copy.deepcopy(question.get("images") or []),
                })
        rows.sort(key=lambda r: (r["paper_name"], r["seq"]))
        return rows

    def submit_attempt(self, attempt):
        paper = self.paper_of(attempt["paper_id"])
        answers = attempt["answers"]
        total = attempt["total"]
        correct = sum(1 for a in answers.values() if a.get("is_correct"))
        score = round(sum(float(a.get("score") or 0) for a in answers.values()), 1)
        duration = int(time.time() - attempt["started"])
        rate = round(correct * 100.0 / total, 1) if total else 0.0
        with self.lock:
            attempt["submitted"] = True
            attempt["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            attempt["score"] = score
            attempt["rate"] = rate
            attempt["correct"] = correct
            attempt["duration"] = duration
            self.finished.append(attempt)
        awarded = 0
        message = "本次未计入个人积分"
        key = (attempt["username"], paper["id"])
        awards = self.points.get(key, 0)
        if attempt.get("points_opt_in") and paper.get("points_enabled") and awards < 3:
            pct = score * 100.0 / paper["max_score"] if paper.get("max_score") else 0
            for rule in paper.get("points_rules") or []:
                if rule["min"] <= pct <= rule["max"]:
                    awarded = int(rule["points"])
                    break
            if awarded > 0:
                self.points[key] = awards + 1
                message = "本次已计入个人积分 +%d 分" % awarded
            else:
                message = "分数未达到计分标准，本次未计入个人积分"
        return {
            "score": score,
            "max_score": paper["max_score"],
            "correct": correct,
            "answered": len(answers),
            "total": total,
            "rate": rate,
            "duration": duration,
            "elapsed": duration,
            "points_enabled": bool(paper.get("points_enabled")),
            "points_awarded": awarded,
            "points_count": self.points.get(key, 0),
            "points_message": message,
            "review_allowed": 0 if paper.get("formal_exam") else 1,
        }

    def review_payload(self, attempt):
        paper = self.paper_of(attempt["paper_id"])
        return {
            "attempt": {
                "id": attempt["id"],
                "score": attempt.get("score", 0),
                "max_score": paper["max_score"],
                "rate": attempt.get("rate", 0),
                "duration": attempt.get("duration", 0),
                "finished_at": attempt.get("finished_at"),
                "mode": attempt["mode"],
            },
            "paper": {"id": paper["id"], "name": paper["name"], "max_score": paper["max_score"]},
            "questions": self.questions_payload(attempt),
            "answers": copy.deepcopy(attempt["answers"]),
        }

    def history(self, user, limit=50):
        rows = []
        for attempt in self.finished:
            if attempt["username"] != user["username"]:
                continue
            paper = self.paper_of(attempt["paper_id"])
            rows.append({
                "id": attempt["id"],
                "paper_name": paper["name"],
                "finished_at": attempt.get("finished_at"),
                "mode": attempt["mode"],
                "formal_exam": bool(paper.get("formal_exam")),
                "score": attempt.get("score", 0),
                "max_score": paper["max_score"],
                "rate": attempt.get("rate", 0),
                "duration": attempt.get("duration", 0),
                "review_allowed": 0 if paper.get("formal_exam") else 1,
            })
        rows.sort(key=lambda r: r.get("finished_at") or "")
        return rows[-int(limit):]

    def stats(self, user):
        rows = self.history(user, 1000)
        answered = 0
        correct = 0
        for attempt in self.attempts.values():
            if attempt["username"] != user["username"]:
                continue
            for record in attempt["answers"].values():
                answered += 1
                if record.get("is_correct"):
                    correct += 1
        accuracy = round(correct * 100.0 / answered, 1) if answered else 0.0
        wrong = len(self.wrong_list(user))
        recent = [{
            "id": row["id"],
            "paper_name": row["paper_name"],
            "finished_at": row["finished_at"],
            "score": row["score"],
            "max_score": row["max_score"],
            "rate": row["rate"],
            "review_allowed": row["review_allowed"],
        } for row in rows[-6:]]
        activity = []
        for offset in range(13, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - offset * 86400))
            count = 0
            for attempt in self.attempts.values():
                if attempt["username"] != user["username"]:
                    continue
                if day == time.strftime("%Y-%m-%d", time.localtime(attempt["started"])):
                    count += len(attempt["answers"])
            activity.append({"date": day, "count": count})
        return {
            "overall": {
                "answered": answered,
                "accuracy": accuracy,
                "wrong": wrong,
                "attempts": len(rows),
            },
            "activity": activity,
            "by_knowledge": [],
            "recent": recent,
        }


def _strip_answer(question):
    question["answer"] = ""
    question["explanation"] = ""
    for sub in question.get("sub_questions") or []:
        _strip_answer(sub)


# --------------------------------------------------------------------------- HTTP
LOGIN_HTML = u"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>统一身份认证 · 登录</title>
<style>body{font-family:system-ui,"Microsoft YaHei";background:#eef2f9;padding:60px}
.card{background:#fff;max-width:360px;margin:0 auto;padding:28px;border-radius:12px;
box-shadow:0 8px 30px rgba(20,40,90,.12)}h1{font-size:18px;margin:0 0 16px}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:10px;
border:1px solid #cbd5e1;border-radius:8px;margin:6px 0 14px}
button{width:100%;padding:11px;border:0;border-radius:8px;background:#2f6fed;color:#fff;
font-size:15px;cursor:pointer}label{font-size:13px;color:#475569}.err{color:#dc2626;font-size:13px}</style>
</head><body>
<div class="card">
<h1>统一身份认证</h1>
__ERROR__
<form method="post" action="/login">
  <input type="hidden" name="csrf_token" value="__CSRF__">
  <input type="hidden" name="next" value="__NEXT__">
  <div>账号</div>
  <input type="text" name="username" id="username" autocomplete="username" placeholder="请输入学号或账号">
  <div>密码</div>
  <input type="password" name="password" id="password" autocomplete="current-password" placeholder="请输入密码">
  <label><input type="checkbox" name="remember" value="1"> 记住我</label>
  <div style="height:12px"></div>
  <button type="submit">登录</button>
</form>
</div></body></html>
"""


class Handler(object):
    """把请求处理逻辑与 HTTP 细节分开，方便测试直接调用。"""

    def __init__(self, db, static_html=None, ui_mode=False):
        self.db = db
        self.static_html = static_html
        self.ui_mode = ui_mode

    # 每个请求返回 (status, content_type, body_bytes, extra_headers)
    def dispatch(self, method, raw_path, headers, body):
        parsed = urllib.parse.urlsplit(raw_path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        sid = _cookie_get(headers.get("Cookie"), "sid")
        if self.db.latency:
            time.sleep(self.db.latency)

        # ---- 登录相关（主系统）
        if path == "/login" and method == "GET":
            return self._login_page(query)
        if path == "/login" and method == "POST":
            return self._login_submit(body)
        if path in ("/logout", "/plugin/examcenter/api/logout"):
            self.db.logout(sid)
            if path == "/logout":
                return 302, "text/plain; charset=utf-8", b"", {"Location": "/login"}
            return 200, "application/json; charset=utf-8", _json_bytes({"ok": True}), {}
        if path == "/api/login" and method == "POST":
            return self._login_json(body)
        if path == "/" or path == "":
            return 302, "text/plain; charset=utf-8", b"", {"Location": BASE_PATH + "/"}
        if path == "/modern_student":
            return 200, "text/html; charset=utf-8", _html_bytes(u"<h1>主系统首页</h1><p>考试中心见 <a href='/plugin/examcenter/'>这里</a></p>"), {}

        # ---- 静态前端（--serve-ui）
        if path == BASE_PATH or path == BASE_PATH + "/":
            page = self.static_html
            if not page:
                page = u"<h1>考试中心 mock</h1><p>接口已就绪：%s/api/</p>" % BASE_PATH
            return 200, "text/html; charset=utf-8", _html_bytes(page), {}
        if path.startswith(BASE_PATH + "/") and ("_files/" in path) and self.ui_mode:
            return self._static_file(path)
        if path.startswith(BASE_PATH + "/") and not path.startswith(BASE_PATH + "/api/"):
            if self.ui_mode and self.static_html:
                return 200, "text/html; charset=utf-8", _html_bytes(self.static_html), {}

        # ---- 插件接口
        if not path.startswith(BASE_PATH + "/api/"):
            return 404, "application/json; charset=utf-8", _json_bytes({"detail": "未找到"}), {}
        api = path[len(BASE_PATH) + len("/api"):]
        user = self.db.user_of(sid)
        if user is None:
            return 401, "application/json; charset=utf-8", _json_bytes({"detail": "未登录"}), {}

        try:
            return self._api(method, api, query, headers, body, user)
        except KeyError as exc:
            return 404, "application/json; charset=utf-8", _json_bytes({"detail": str(exc.args[0]) if exc.args else "未找到"}), {}
        except ValueError as exc:
            return 400, "application/json; charset=utf-8", _json_bytes({"detail": str(exc)}), {}

    # ------------------------------------------------------------- 登录实现
    def _login_page(self, query):
        next_url = (query.get("next") or [BASE_PATH + "/"])[0]
        error = ""
        if (query.get("e") or [""])[0]:
            error = u'<div class="err">账号或密码不正确</div>'
        html = LOGIN_HTML.replace("__CSRF__", uuid.uuid4().hex)
        html = html.replace("__NEXT__", next_url).replace("__ERROR__", error)
        return 200, "text/html; charset=utf-8", _html_bytes(html), {}

    def _login_submit(self, body):
        fields = _parse_form(body)
        username = (fields.get("username") or "").strip()
        password = fields.get("password") or ""
        next_url = fields.get("next") or (BASE_PATH + "/")
        sid = self.db.login(username, password)
        if not sid:
            return 302, "text/plain; charset=utf-8", b"", {"Location": "/login?e=1"}
        return 302, "text/plain; charset=utf-8", b"", {
            "Location": next_url,
            "Set-Cookie": "sid=%s; Path=/; HttpOnly" % sid,
        }

    def _login_json(self, body):
        try:
            payload = json.loads(_decode_body(body) or "{}")
        except Exception:
            return 400, "application/json; charset=utf-8", _json_bytes({"detail": "请求体不是 JSON"}), {}
        username = str(payload.get("username") or payload.get("user") or "").strip()
        password = str(payload.get("password") or payload.get("pwd") or "")
        sid = self.db.login(username, password)
        if not sid:
            return 401, "application/json; charset=utf-8", _json_bytes({"detail": "账号或密码不正确"}), {}
        return 200, "application/json; charset=utf-8", _json_bytes({"ok": True}), {
            "Set-Cookie": "sid=%s; Path=/; HttpOnly" % sid,
        }

    # ------------------------------------------------------------- 静态文件
    def _static_file(self, path):
        name = path.rsplit("/", 1)[-1]
        candidates = [
            os.path.join(ROOT, STATIC_DIR_NAME, name),
            os.path.join(ROOT, STATIC_DIR_NAME, name.replace(".下载", ".下载")),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                with io.open(candidate, "rb") as fh:
                    data = fh.read()
                ctype = "text/css" if name.endswith(".css") else (
                    "image/png" if name.endswith(".png") else "application/javascript")
                return 200, ctype + "; charset=utf-8", data, {}
        return 404, "text/plain; charset=utf-8", b"not found", {}

    # ------------------------------------------------------------- 插件接口
    def _api(self, method, api, query, headers, body, user):
        def ok(obj):
            return 200, "application/json; charset=utf-8", _json_bytes(obj), {}

        if api == "/me" and method == "GET":
            return ok({"user": {
                "id": user["id"],
                "name": user["name"],
                "username": user["username"],
                "class_name": user.get("class_name") or "",
                "role": user.get("role") or "student",
                "must_change": False,
                "home": "/modern_student",
            }})

        if api == "/papers" and method == "GET":
            return ok({"items": self.db.visible_papers(user)})

        if api == "/attempts" and method == "GET":
            limit = int((query.get("limit") or ["50"])[0])
            return ok({"items": self.db.history(user, limit)})

        if api == "/attempts" and method == "POST":
            payload = _json_body(body)
            paper = self.db.paper_of(payload.get("paper_id") or 0)
            if paper is None:
                return 404, "application/json; charset=utf-8", _json_bytes({"detail": "试卷不存在"}), {}
            mode = payload.get("mode") if payload.get("mode") in ("exam", "practice", "wrong") else "exam"
            attempt, resumed = self.db.start_attempt(
                user, paper, mode,
                bool(payload.get("fresh")),
                payload.get("points_opt_in"),
            )
            elapsed = int(time.time() - attempt["started"])
            return ok({
                "attempt": {
                    "id": attempt["id"],
                    "paper_id": paper["id"],
                    "paper_name": paper["name"],
                    "total": attempt["total"],
                    "max_score": paper["max_score"],
                    "shuffle": paper.get("shuffle", False),
                    "mode": attempt["mode"],
                },
                "questions": self.db.questions_payload(attempt),
                "answers": copy.deepcopy(attempt["answers"]),
                "elapsed": elapsed,
                "cursor": attempt.get("cursor", 0),
                "resumed": resumed,
            })

        match = re.match(r"^/attempts/(\d+)$", api)
        if match and method == "GET":
            attempt = self.db.attempts.get(int(match.group(1)))
            if attempt is None:
                attempt = self._find_finished(int(match.group(1)))
            if attempt is None:
                raise KeyError("作答记录不存在")
            if attempt["username"] != user["username"]:
                raise KeyError("作答记录不存在")
            paper = self.db.paper_of(attempt["paper_id"])
            if paper.get("formal_exam"):
                return 403, "application/json; charset=utf-8", _json_bytes({"detail": "正式考试不提供答案与解析"}), {}
            return ok(self.db.review_payload(attempt))

        match = re.match(r"^/attempts/(\d+)/answer$", api)
        if match and method == "POST":
            attempt = self.db.attempts.get(int(match.group(1)))
            if attempt is None or attempt["submitted"]:
                raise KeyError("作答不存在或已交卷")
            if attempt["username"] != user["username"]:
                raise KeyError("作答不存在")
            payload = _json_body(body)
            return ok(self.db.answer_question(attempt, int(payload.get("question_id") or 0),
                                              payload.get("answer")))

        match = re.match(r"^/attempts/(\d+)/beat$", api)
        if match and method == "POST":
            attempt = self.db.attempts.get(int(match.group(1)))
            if attempt is None:
                raise KeyError("作答不存在")
            try:
                payload = _json_body(body)
            except Exception:
                payload = {}
            attempt["cursor"] = int(payload.get("index") or 0)
            return ok({"ok": True, "elapsed": int(time.time() - attempt["started"])})

        match = re.match(r"^/attempts/(\d+)/submit$", api)
        if match and method == "POST":
            attempt = self.db.attempts.get(int(match.group(1)))
            if attempt is None:
                raise KeyError("作答不存在")
            if attempt["username"] != user["username"]:
                raise KeyError("作答不存在")
            if attempt["submitted"]:
                raise ValueError("已经交卷了")
            summary = self.db.submit_attempt(attempt)
            return ok({"summary": summary})

        if api == "/wrong" and method == "GET":
            # 真实服务器支持 limit/offset（实测），靶场照做，否则契约文档会说谎
            items = self.db.wrong_list(user)
            limit = _query_int(query, "limit")
            offset = _query_int(query, "offset")
            if offset > 0:
                items = items[offset:]
            if limit > 0:
                items = items[:limit]
            return ok({"items": items})

        if api == "/stats/me" and method == "GET":
            return ok(self.db.stats(user))

        if api == "/me/password" and method == "POST":
            return ok({"ok": True})

        return 404, "application/json; charset=utf-8", _json_bytes({"detail": "接口不存在：%s" % api}), {}

    def _find_finished(self, aid):
        for attempt in self.db.finished:
            if attempt["id"] == int(aid):
                return attempt
        return None


def _query_int(query, name, default=0):
    """从 parse_qs 结果里取一个整型查询参数，取不到就用默认值。"""
    try:
        raw = (query.get(name) or [""])[0]
    except Exception:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _html_bytes(text):
    return text.encode("utf-8")


def _decode_body(body):
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body or ""


def _json_body(body):
    text = _decode_body(body).strip()
    if not text:
        raise ValueError("请求体为空")
    try:
        return json.loads(text)
    except Exception:
        raise ValueError("请求体不是合法 JSON")


def _parse_form(body):
    text = _decode_body(body)
    fields = {}
    for key, values in urllib.parse.parse_qs(text, keep_blank_values=True).items():
        fields[key] = values[0] if values else ""
    return fields


def _cookie_get(cookie_header, name):
    for chunk in str(cookie_header or "").split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return None


# --------------------------------------------------------------------------- 服务器
def make_handler_class(handler):
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "examcenter-mock/0.1"
        sys_version = ""

        def log_message(self, fmt, *args):
            if handler.db.verbose:
                sys.stderr.write("[mock] %s %s\n" % (self.address_string(), fmt % args))

        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                status, ctype, payload, extra = handler.dispatch(
                    method, self.path, self.headers, body
                )
            except Exception as exc:  # 兜底，别让测试线程挂掉
                status, ctype, payload, extra = (
                    500, "application/json; charset=utf-8",
                    _json_bytes({"detail": "mock 内部错误：%s" % exc}), {},
                )
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

    return _Handler


def build_users(account_specs=None):
    """默认给一个和你手工测试一致的账号：demo001 / 123456。"""
    specs = account_specs or [
        {"username": "demo001", "password": "123456", "name": "测试用户"},
    ]
    users = {}
    for index, spec in enumerate(specs):
        username = spec["username"]
        users[username] = {
            "id": 100 + index,
            "username": username,
            "name": spec.get("name") or _display_name(username),
            "class_name": spec.get("class_name") or "高三(1)班",
            "role": spec.get("role") or "student",
            "password": spec.get("password") or "123456",
        }
    return users


def _display_name(username):
    digits = ""
    rest = ""
    for ch in username:
        if ch.isdigit() and not rest:
            digits += ch
        else:
            rest += ch
    return rest or username


def build(question_budget=12, paper_count=2, leak_answers=False, latency=0.0,
          verbose=False, accounts=None, ui_mode=False):
    papers = seedmod.build_papers(question_budget=question_budget, paper_count=paper_count)
    db = Database(papers, build_users(accounts), leak_answers=leak_answers,
                  latency=latency, verbose=verbose)
    static_html = None
    if ui_mode:
        for name in STATIC_HTML_NAMES:
            candidate = os.path.join(ROOT, name)
            if os.path.isfile(candidate):
                with io.open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                    static_html = fh.read()
                break
    return db, Handler(db, static_html=static_html, ui_mode=ui_mode)


def start_server(port=8420, host="127.0.0.1", question_budget=12, paper_count=2,
                 leak_answers=False, latency=0.0, verbose=False, accounts=None,
                 ui_mode=False):
    """起一个后台线程服务器；port=0 时由系统分配。返回 (server, thread, db)。"""
    from http.server import ThreadingHTTPServer

    db, handler = build(question_budget=question_budget, paper_count=paper_count,
                        leak_answers=leak_answers, latency=latency, verbose=verbose,
                        accounts=accounts, ui_mode=ui_mode)
    server = ThreadingHTTPServer((host, port), make_handler_class(handler))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="mock-server")
    thread.daemon = True
    thread.start()
    return server, thread, db


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description="考试中心本地契约服务器（离线靶场）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420, help="0 表示随机端口")
    parser.add_argument("--questions", type=int, default=12, help="每套卷题数")
    parser.add_argument("--papers", type=int, default=2, help="卷子数量")
    parser.add_argument("--leak-answers", action="store_true",
                        help="考试模式下也在 /answer 里返回正确答案（用于测试捷径路径）")
    parser.add_argument("--latency", type=float, default=0.0, help="每个请求的模拟延迟（秒）")
    parser.add_argument("--serve-ui", action="store_true", help="把保存的真实前端挂上去")
    parser.add_argument("--accounts", default="demo001:123456",
                        help="账号清单：账号:密码,账号:密码")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    accounts = []
    for chunk in str(args.accounts).replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        username, _, password = chunk.partition(":")
        accounts.append({"username": username, "password": password or "123456"})

    port = args.port or _free_port()
    server, thread, db = start_server(
        port=port, host=args.host, question_budget=args.questions,
        paper_count=args.papers, leak_answers=args.leak_answers,
        latency=args.latency, verbose=args.verbose, accounts=accounts,
        ui_mode=args.serve_ui,
    )
    base = "http://%s:%d" % (args.host, port)
    print("")
    print("考试中心 mock 已启动")
    print("  主页      : %s/plugin/examcenter/" % base)
    print("  登录页    : %s/login" % base)
    print("  接口前缀  : %s/plugin/examcenter/api/" % base)
    print("  账号      : %s" % ", ".join(u["username"] + " / " + u["password"] for u in db.users.values()))
    print("  卷子      : %s" % ", ".join("%s(id=%s, %d题)" % (p["name"], p["id"], p["total"]) for p in db.papers))
    print("  考试模式泄答案: %s" % ("是" if db.leak_answers else "否"))
    print("")
    print("按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("")
        print("正在停止……")
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
