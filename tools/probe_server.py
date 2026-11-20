# -*- coding: utf-8 -*-
"""在线实测脚本：只核对交接文档 §6 的确认项，绝不交卷、绝不跑全卷。

    python tools/probe_server.py --account demo001 --paper 2
    python tools/probe_server.py --account demo001 --paper 2 --max-questions 3

它做的事（每一步单独兜异常，一步失败不影响下一步）：

    1. 登录，并把登录表单的 action/method/字段名/隐藏域/验证码线索记下来   -> §6③
    2. GET /api/papers，定位目标试卷
    3. POST /api/attempts（默认 fresh=false，不动已有进度）
       检查题目对象里有没有非空 answer                                      -> §6①
       顺便扫描响应里的时限/倒计时字段                                       -> §6④
    4. 只挑 N 道选择题提交探针答案：
       看 /answer 是否回 correct_answer / is_correct                        -> §6②
       看 /api/wrong 是否收录、答案对不对、改对后是否立刻消失                 -> §6⑥
       /api/wrong 的总条数也记一笔                                          -> §6⑤
    5. 到此为止。本脚本不含任何 /submit 调用。

结果同时写到 status/probe-result.json 与 logs/probe-raw/*.json。
"""

import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import api as apimod          # noqa: E402
from autoexam import config as configmod    # noqa: E402
from autoexam import login as loginmod      # noqa: E402
from autoexam import matcher                # noqa: E402
from autoexam import netio                  # noqa: E402
from autoexam import ui                     # noqa: E402

CAPTCHA_WORDS = ["验证码", "captcha", "Captcha", "CAPTCHA", "kaptcha", "recaptcha"]
TIMING_KEYS = ("limit", "duration", "deadline", "countdown", "remain", "seconds",
               "second", "time_limit", "timelimit", "expire", "left_time", "lefttime")


def _slug(text):
    return "".join(ch if ch.isalnum() else "-" for ch in str(text or ""))[:40] or "login"


def _preview(text, width=40):
    return matcher.stem_preview(text, width)


def _walk_keys(node, prefix="", depth=0, out=None):
    """递归收集所有键路径（只看键名，不看值）。"""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            path = ("%s.%s" % (prefix, key)).strip(".")
            out.append(path)
            if isinstance(value, (dict, list)):
                _walk_keys(value, path, depth + 1, out)
    elif isinstance(node, list):
        for item in node[:3]:
            _walk_keys(item, prefix + "[]", depth + 1, out)
    return out


def _timing_hits(node, prefix=""):
    hits = []
    for path in _walk_keys(node, prefix):
        low = path.lower()
        if any(token in low for token in TIMING_KEYS):
            hits.append(path)
    return sorted(set(hits))


def analyze_login_page(cfg, account):
    """把登录页快照拆开，回答 §6③。"""
    path = os.path.join(cfg.get("log_dir") or "logs", "login-%s.html" % _slug(account))
    info = {"snapshot": path, "exists": os.path.isfile(path)}
    if not info["exists"]:
        return info
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
    except Exception as exc:
        info["error"] = "读取失败：%s" % exc
        return info

    info["bytes"] = len(html)
    info["captcha_words"] = [word for word in CAPTCHA_WORDS if word in html]
    forms = loginmod.parse_forms(html)
    info["form_count"] = len(forms)
    form = loginmod.pick_login_form(forms)
    if not form:
        info["login_form"] = None
        info["note"] = "登录页里没有含 password 的 form（可能是 SPA，接口登录）"
        return info
    user_field, pass_field = loginmod.guess_fields(form)
    info["login_form"] = {
        "action": form.get("action") or "",
        "method": form.get("method") or "",
        "enctype": form.get("enctype") or "",
        "user_field": user_field,
        "pass_field": pass_field,
        "fields": [
            {"name": item["name"], "type": item["type"], "tag": item["tag"]}
            for item in form["inputs"] if item["name"]
        ],
        "hidden_fields": [
            item["name"] for item in form["inputs"]
            if item["type"] == "hidden" and item["name"]
        ],
        "has_csrf": any(
            ("csrf" in (item["name"] or "").lower()
             or "token" in (item["name"] or "").lower())
            for item in form["inputs"]
        ),
    }
    return info


def pick_probe(question, known_answer):
    """挑一个肯定不是正确答案的选项（已知答案时），否则随机挑。"""
    import random
    keys = apimod.option_keys(question)
    if not keys:
        return None
    known = str(known_answer or "").strip().upper()
    wrong = [key for key in keys if key.upper() not in known] if known else keys
    if not wrong:
        wrong = keys
    count = 1
    if question.get("type") == "multi" and len(wrong) >= 2:
        count = 1  # 多选题只挑 1 个，保证是错的
    chosen = sorted(random.sample(wrong, count))
    return "".join(chosen)


def probe_one(api, logger, attempt_id, paper_name, slot, wrong_index, poll_interval):
    """对一道题做「探针 -> 查错题本 -> 回填 -> 再查」的完整语义测试。"""
    question = slot["q"]
    qid = int(question["id"])
    known = str(question.get("answer") or "").strip()
    probe = pick_probe(question, known)
    entry = {
        "question_id": qid,
        "seq": question.get("seq"),
        "type": question.get("type"),
        "stem": _preview(question.get("stem")),
        "option_keys": apimod.option_keys(question),
        "leaked_answer": known,
        "probe": probe,
    }
    if not probe:
        entry["skipped"] = "没有选项，跳过（填空题不参与本题型实验）"
        return entry

    try:
        reply = api.answer(attempt_id, qid, probe)
    except Exception as exc:
        entry["error"] = "提交探针失败：%s" % exc
        return entry

    reply = reply if isinstance(reply, dict) else {}
    entry["answer_response_keys"] = sorted(reply.keys())
    entry["is_correct_field"] = reply.get("is_correct")
    entry["correct_answer_field"] = str(reply.get("correct_answer") or "")
    entry["explanation_len"] = len(str(reply.get("explanation") or ""))

    time.sleep(poll_interval)
    try:
        items = api.wrong()
    except Exception as exc:
        entry["wrong_read_error"] = str(exc)
        return entry

    index = matcher.WrongBookIndex(items)
    result = index.find(paper_name, question.get("stem"))
    entry["wrong_total"] = len(items)
    entry["listed_after_probe"] = result["status"]
    entry["wrongbook_answer"] = result["answer"]
    if result["item"]:
        entry["wrong_times"] = result["item"].get("wrong_times")
        entry["wrongbook_paper_name"] = result["item"].get("paper_name")
        entry["wrongbook_stem"] = _preview(result["item"].get("stem"))
        entry["wrongbook_option_count"] = len(result["item"].get("options") or [])

    if result["status"] == "found" and result["answer"]:
        correct = result["answer"]
        entry["recovered_answer"] = correct
        try:
            reply2 = api.answer(attempt_id, qid, correct)
        except Exception as exc:
            entry["refill_error"] = str(exc)
            return entry
        reply2 = reply2 if isinstance(reply2, dict) else {}
        entry["refill_response_keys"] = sorted(reply2.keys())
        entry["refill_correct_answer_field"] = str(reply2.get("correct_answer") or "")
        time.sleep(poll_interval)
        try:
            items2 = api.wrong()
            index2 = matcher.WrongBookIndex(items2)
            result2 = index2.find(paper_name, question.get("stem"))
            entry["listed_after_correct"] = result2["status"]
            entry["cleared_after_correct"] = (result2["status"] == "missing")
            entry["wrong_total_after_correct"] = len(items2)
        except Exception as exc:
            entry["wrong2_error"] = str(exc)
    return entry


def main(argv=None):
    parser = configmod.build_parser()
    parser.add_argument("--max-questions", dest="max_questions", type=int, default=3,
                        help="最多拿几道题做探针语义测试（默认 3，别把整套卷做掉）")
    parser.add_argument("--poll-wait", dest="poll_wait", type=float, default=1.2,
                        help="每次查错题本前的等待秒数（默认 1.2）")
    args = parser.parse_args(argv)
    raw = vars(args)
    cfg = configmod.load_config(raw.get("config"), raw)

    logger = ui.Logger(os.path.join(cfg.get("log_dir") or "logs",
                                    "probe-%s.log" % time.strftime("%Y%m%d")),
                       prefix="实测")
    account = (cfg.get("account") or "").strip()
    password = cfg.get("password") or "123456"
    paper_id = cfg.get("paper")
    max_questions = max(1, int(raw.get("max_questions") or 3))
    poll_wait = float(raw.get("poll_wait") or 1.2)

    ui.banner(logger, "在线实测（只探测，不交卷）", [
        "服务器  %s" % (cfg.get("base_url") or "（没找到）"),
        "账号    %s" % (account or "（没给 --account）"),
        "试卷    %s" % (paper_id if paper_id is not None else "（没给 --paper）"),
        "题量上限 %d 道" % max_questions,
    ])
    if not cfg.get("base_url"):
        logger.error("没有服务器地址。用 --base 指定。")
        return 2
    if not account:
        logger.error("必须给 --account。")
        return 2

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": cfg.get("base_url"),
        "account": account,
        "paper_id": paper_id,
        "max_questions": max_questions,
        "submitted": False,
    }

    # ---------------------------------------------------------------- 1 登录
    logger.info("-- 1/5 登录 --")
    client = netio.HttpClient(
        cfg.get("base_url"),
        timeout=cfg.get("http_timeout") or 15,
        retries=cfg.get("retries") or 3,
        save_raw=True,
        raw_dir=os.path.join(cfg.get("log_dir") or "logs", "probe-raw"),
        logger=logger,
        tag_prefix="probe",
        insecure_tls=bool(cfg.get("insecure_tls")),
        proxy=cfg.get("proxy") or None,
    )
    api = apimod.ExamCenter(client, logger)
    try:
        user = loginmod.login(cfg, client, logger, account, password)
    except netio.NetworkError as exc:
        logger.error("连不上服务器：%s" % exc)
        return 1
    except loginmod.LoginError as exc:
        logger.error("登录失败：%s" % exc)
        return 1
    report["login"] = {
        "user": user,
        "cookies": sorted(client.cookies.keys()),
        "form": analyze_login_page(cfg, account),
    }

    # ------------------------------------------------------------- 2 试卷列表
    logger.info("-- 2/5 试卷列表 --")
    try:
        papers = api.papers()
    except Exception as exc:
        logger.error("取试卷列表失败：%s" % exc)
        return 1
    report["papers"] = [
        {
            "id": paper.get("id"),
            "name": paper.get("name"),
            "total": paper.get("total"),
            "max_score": paper.get("max_score"),
            "formal_exam": paper.get("formal_exam"),
            "exam_open": paper.get("exam_open"),
            "practice_open": paper.get("practice_open"),
            "points_enabled": paper.get("points_enabled"),
            "points_awards": paper.get("points_awards"),
            "shuffle": paper.get("shuffle"),
            "doing": paper.get("doing"),
        }
        for paper in papers
    ]
    for paper in papers:
        logger.plain("  " + apimod.paper_line(paper))

    target = None
    if paper_id is not None:
        for paper in papers:
            if int(paper.get("id") or 0) == int(paper_id):
                target = paper
                break
    if target is None:
        logger.error("试卷列表里没有 id=%s" % paper_id)
        return 1
    if target.get("formal_exam"):
        logger.error("目标卷是正式考试卷，本脚本不动它。")
        return 1
    report["paper"] = {"id": target.get("id"), "name": target.get("name"),
                       "total": target.get("total"), "max_score": target.get("max_score")}
    logger.ok("目标试卷：%s（id=%s，%s 题）" %
              (target.get("name"), target.get("id"), target.get("total")))

    # --------------------------------------------------------- 3 开始/恢复作答
    logger.info("-- 3/5 打开作答（fresh=false，不动已有进度；不交卷）--")
    try:
        data = api.start_attempt(int(paper_id), cfg.get("mode") or "exam",
                                 fresh=False, points_opt_in=None)
    except Exception as exc:
        logger.error("打开作答失败：%s" % exc)
        return 1
    data = data if isinstance(data, dict) else {}
    attempt = data.get("attempt") or {}
    attempt_id = int(attempt.get("id") or 0)
    report["attempt"] = {
        "id": attempt_id,
        "resumed": data.get("resumed"),
        "elapsed": data.get("elapsed"),
        "cursor": data.get("cursor"),
        "top_level_keys": sorted(data.keys()),
        "attempt_keys": sorted(attempt.keys()),
        "answers_seen": len(data.get("answers") or {}),
        "timing_key_hits": _timing_hits(data, "attempt"),
    }
    logger.ok("attempt=%s  resumed=%s  elapsed=%s" %
              (attempt_id, data.get("resumed"), data.get("elapsed")))

    # ------------------------------------------------------- 4 答案泄露 (§6①)
    logger.info("-- 4/5 检查题目是否自带答案（§6①）+ 时限字段（§6④）--")
    slots = apimod.flatten_questions(data.get("questions"))
    leak_samples = []
    with_answer = 0
    for slot in slots:
        answer = str(slot["q"].get("answer") or "").strip()
        if answer:
            with_answer += 1
            if len(leak_samples) < 5:
                leak_samples.append({
                    "question_id": slot["q"].get("id"),
                    "seq": slot["q"].get("seq"),
                    "type": slot["q"].get("type"),
                    "stem": _preview(slot["q"].get("stem")),
                    "answer": answer,
                })
    report["leak"] = {
        "questions": len(slots),
        "with_answer": with_answer,
        "samples": leak_samples,
    }
    if with_answer:
        logger.warn("题目接口带答案：%d/%d 道非空（走捷径，最快路径）" %
                    (with_answer, len(slots)))
        for sample in leak_samples[:3]:
            logger.plain("    qid=%s %s -> %s" %
                         (sample["question_id"], sample["stem"], sample["answer"]))
    else:
        logger.ok("题目接口不带答案（%d 道全为空，走错题本流程）" % len(slots))

    # --------------------------------------------------- 5 探针语义 (§6②⑤⑥)
    logger.info("-- 5/5 探针语义测试（最多 %d 道，不交卷）--" %
                max_questions)
    try:
        wrong_items = api.wrong()
    except Exception as exc:
        logger.warn("取错题本失败：%s" % exc)
        wrong_items = []
    index = matcher.WrongBookIndex(wrong_items)
    report["wrongbook_before"] = {
        "total": len(wrong_items),
        "papers": sorted(set(str(item.get("paper_name") or "") for item in wrong_items)),
        "max_wrong_times": max([int(item.get("wrong_times") or 0) for item in wrong_items] or [0]),
    }
    logger.info("错题本当前 %d 条（覆盖 %d 套卷）" %
                (len(wrong_items), len(report["wrongbook_before"]["papers"])))

    candidates = []
    for slot in slots:
        question = slot["q"]
        if question.get("type") not in ("single", "judge"):
            continue
        if not apimod.option_keys(question):
            continue
        found = index.find(target.get("name"), question.get("stem"))
        if found["status"] != "missing":
            continue
        candidates.append(slot)
    logger.info("可用于实验的题（单选/判断且当前不在错题本）：%d 道" % len(candidates))

    results = []
    for slot in candidates[:max_questions]:
        label = "[%s/%s]" % (slot["q"].get("seq"), len(slots))
        try:
            entry = probe_one(api, logger, attempt_id, target.get("name"),
                              slot, index, poll_wait)
        except Exception as exc:
            entry = {"error": "意外失败：%s" % exc,
                     "question_id": slot["q"].get("id")}
        results.append(entry)
        logger.plain("  %s qid=%s %s -> 探针=%s 收录=%s 回填=%s 还原=%s" % (
            label, entry.get("question_id"), entry.get("stem"),
            entry.get("probe"), entry.get("listed_after_probe"),
            entry.get("recovered_answer") or "-",
            entry.get("cleared_after_correct")))
    report["probe_results"] = results

    # ------------------------------------------------------------- 结论汇总
    any_answer_api = any(str(item.get("correct_answer_field") or "").strip()
                         for item in results)
    listed = [item for item in results if item.get("listed_after_probe") == "found"]
    cleared = [item for item in results if item.get("cleared_after_correct") is True]
    not_cleared = [item for item in results if item.get("cleared_after_correct") is False]

    report["verdicts"] = {
        "q1_questions_leak_answer": bool(with_answer),
        "q2_answer_api_returns_correct": any_answer_api,
        "q4_timing_fields": report["attempt"]["timing_key_hits"],
        "q5_wrong_total": len(wrong_items),
        "q6_wrong_entry_added": bool(listed),
        "q6_wrong_entry_removed_after_correct": bool(cleared) and not not_cleared,
        "submitted": False,
    }

    logger.plain("")
    logger.plain("=" * 66)
    logger.plain("  实测结论")
    logger.plain("-" * 66)
    logger.plain("  ① 题目自带答案        %s" % _yesno(bool(with_answer)))
    logger.plain("  ② /answer 回正确答案  %s" % _yesno(any_answer_api))
    logger.plain("  ④ 时限/倒计时字段     %s" % (
        "、".join(report["attempt"]["timing_key_hits"]) or "没发现"))
    logger.plain("  ⑤ 错题本总条数        %d 条" % len(wrong_items))
    logger.plain("  ⑥ 探针答错后在册      %s（%d/%d 道）" % (
        _yesno(bool(listed)), len(listed), len(results)))
    logger.plain("  ⑥ 改对后立刻移出      %s（%d/%d 道）" % (
        _yesno(bool(cleared) and not not_cleared), len(cleared), len(listed)))
    logger.plain("-" * 66)
    logger.plain("  未调用 /submit，成绩没有入账。attempt=%s 仍处于未完成状态。" % attempt_id)
    logger.plain("=" * 66)
    logger.plain("")

    out_path = os.path.join(cfg.get("status_dir") or "status", "probe-result.json")
    try:
        ui.ensure_dir(os.path.dirname(os.path.abspath(out_path)))
        with io.open(out_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False, indent=1))
        logger.info("完整结果：%s" % out_path)
    except Exception as exc:
        logger.warn("写结果文件失败：%s" % exc)

    raw_dir = os.path.join(cfg.get("log_dir") or "logs", "probe-raw")
    logger.info("原始响应：%s" % os.path.abspath(raw_dir))
    return 0


def _yesno(value):
    return "是" if value else "否"


if __name__ == "__main__":
    sys.exit(main())
