# -*- coding: utf-8 -*-
"""本地自检：只用原始 urllib 走一遍 mock 的接口，验证契约本身没问题。

这个脚本故意不复用 autoexam 包，这样如果客户端写错了，这里还能当参照物。
    python tools/smoke.py
"""

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mock import server as mockserver  # noqa: E402


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    srv, thread, db = mockserver.start_server(port=0, question_budget=8, paper_count=2)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    cookies = {}

    def call(path, method="GET", body=None, form=None):
        url = base + path
        data = None
        headers = {"User-Agent": "smoke/0.1", "Accept": "application/json"}
        if cookies:
            headers["Cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            resp = opener.open(req, timeout=10)
            status, hdrs, raw = resp.getcode(), resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            status, hdrs, raw = err.code, err.headers, err.read()
        for item in hdrs.get_all("Set-Cookie") or []:
            name, _, value = item.split(";", 1)[0].partition("=")
            cookies[name.strip()] = value.strip()
        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except Exception:
            payload = text
        return status, hdrs, payload

    failures = []

    def check(label, condition, extra=""):
        mark = "OK  " if condition else "FAIL"
        print("[%s] %s %s" % (mark, label, extra))
        if not condition:
            failures.append(label)

    try:
        print("mock 地址:", base)
        status, _, page = call("/login?next=/plugin/examcenter/")
        check("登录页 200", status == 200 and "password" in str(page))

        status, _, _ = call("/login", "POST", form={
            "username": "demo001", "password": "123456",
            "next": "/plugin/examcenter/", "csrf_token": "x",
        })
        check("登录 302 + 拿到 cookie", status == 302 and "sid" in cookies, cookies.get("sid", "")[:8])

        status, _, me = call("/plugin/examcenter/api/me")
        check("api/me", status == 200 and me["user"]["username"] == "demo001", me)

        status, _, papers = call("/plugin/examcenter/api/papers")
        check("api/papers", status == 200 and len(papers["items"]) >= 2,
              [p["name"] + "/" + str(p["total"]) for p in papers["items"]])

        paper = papers["items"][0]
        status, _, started = call("/plugin/examcenter/api/attempts", "POST", body={
            "paper_id": paper["id"], "mode": "exam", "fresh": True, "points_opt_in": True,
        })
        check("POST api/attempts", status == 200 and started["attempt"]["id"] > 0,
              "aid=%s questions=%d" % (started["attempt"]["id"], len(started["questions"])))

        aid = started["attempt"]["id"]
        leaves = []

        def collect(questions):
            for question in questions:
                subs = question.get("sub_questions") or []
                if question.get("type") == "comprehensive" and subs:
                    collect(subs)
                else:
                    leaves.append(question)

        collect(started["questions"])
        check("题目扁平化", len(leaves) == paper["total"], "%d/%d" % (len(leaves), paper["total"]))
        check("考试模式不泄露答案", all(not q.get("answer") for q in leaves))

        first = leaves[0]
        correct_answer = db.leaf_index[first["id"]][1].get("answer") or ""
        wrong_value = _wrong_answer(first, correct_answer)
        status, _, reply = call("/plugin/examcenter/api/attempts/%d/answer" % aid, "POST",
                                body={"question_id": first["id"], "answer": wrong_value})
        check("提交探针答案", status == 200 and reply.get("hidden") is True, reply)

        status, _, wrongs = call("/plugin/examcenter/api/wrong")
        check("错题本收录", status == 200 and any(w["id"] == first["id"] for w in wrongs["items"]),
              "共 %d 条" % len(wrongs["items"]))
        entry = [w for w in wrongs["items"] if w["id"] == first["id"]]
        check("错题本带正确答案", bool(entry) and bool(entry[0]["answer"]),
              entry[0]["answer"] if entry else "")

        if entry:
            status, _, fixed = call("/plugin/examcenter/api/attempts/%d/answer" % aid, "POST",
                                    body={"question_id": first["id"], "answer": entry[0]["answer"]})
            check("回填正确答案", status == 200, fixed)
            status, _, wrongs = call("/plugin/examcenter/api/wrong")
            check("答对后移出错题本", not any(w["id"] == first["id"] for w in wrongs["items"]))

        status, _, beat = call("/plugin/examcenter/api/attempts/%d/beat" % aid, "POST",
                               body={"index": 3})
        check("心跳 /beat", status == 200, beat)

        status, _, submitted = call("/plugin/examcenter/api/attempts/%d/submit" % aid, "POST")
        check("交卷", status == 200 and "summary" in submitted, submitted.get("summary"))
        status, _, review = call("/plugin/examcenter/api/attempts/%d" % aid)
        check("作答回顾", status == 200 and review["paper"]["name"] == paper["name"])
        status, _, history = call("/plugin/examcenter/api/attempts?limit=10")
        check("答题记录", status == 200 and len(history["items"]) == 1)
        status, _, stats = call("/plugin/examcenter/api/stats/me")
        check("我的统计", status == 200 and stats["overall"]["answered"] >= 1, stats["overall"])
        status, _, _ = call("/plugin/examcenter/api/logout", "POST")
        check("退出接口", status == 200)
        status, _, _ = call("/plugin/examcenter/api/me")
        check("退出后 401", status == 401, status)
    finally:
        srv.shutdown()
        srv.server_close()

    print("")
    if failures:
        print("失败项：", "、".join(failures))
        return 1
    print("全部通过。")
    return 0


def _wrong_answer(question, correct_answer):
    """给一道题造一个一定错的选择（显然不依赖题目是否泄露答案）。"""
    qtype = question.get("type")
    if qtype in ("single", "judge", "multi") and question.get("options"):
        correct = set(str(correct_answer or "").upper())
        for option in reversed(question["options"]):
            if option["key"] not in correct:
                return option["key"]
        return question["options"][0]["key"]
    return "X"


if __name__ == "__main__":
    sys.exit(main())
