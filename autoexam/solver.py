# -*- coding: utf-8 -*-
"""单账号刷题状态机。

默认策略 per-question（一题一轮）：
    探针乱选 → 查错题本拿正确答案 → 回填 → 下一题
每一题的总耗时会被补齐到 [delay_min, delay_max] 秒区间内，模拟人工节奏。

另有两种捷径，服务端支持时会自动启用（都会写进日志，便于交接时核对）：
    - 题目自带 answer 字段（student.js:242 暗示服务端会带）：直接答对，最省时间；
    - /answer 返回 correct_answer：探针一次即可拿到答案，免去错题本轮询。

batch 策略：先把所有题探针一遍，再拉一次错题本批量回填，最后交卷。
倒计时吃紧时用得上（--strategy batch 或 --assume-limit 算出来时间不够时）。
"""

import random
import threading
import time

from . import api as apimod
from . import matcher
from . import netio


class AttemptFailed(Exception):
    """无法继续作答（不是网络问题，是逻辑上不能继续）。"""


class Solver(object):
    def __init__(self, cfg, client_api, logger, username, paper, cache,
                 on_progress=None, relogin=None):
        self.cfg = cfg
        self.api = client_api
        self.logger = logger
        self.username = username
        self.paper = paper or {}
        self.cache = cache
        self.on_progress = on_progress or (lambda kind, payload: None)
        self.relogin = relogin

        self.attempt_id = 0
        self.paper_name = self.paper.get("name") or ""
        self.paper_id = int(self.paper.get("id") or 0)
        self.slots = []
        self._index = 0
        self._stop = threading.Event()
        self._beat_thread = None

        self.trust_answer_api = self._flag(cfg.get("trust_answer_api"))
        self.trust_questions = self._flag(cfg.get("trust_questions_answer"))

        self.counts = {
            "cache": 0, "wrongbook": 0, "answer_api": 0, "questions": 0,
            "unknown": 0, "ambiguous": 0, "failed": 0,
        }
        self.unresolved = []
        self._probe_records = {}
        self._wrong_cache = None
        self._wrong_ts = 0.0

    # ------------------------------------------------------------- 小工具
    @staticmethod
    def _flag(value):
        """auto/on/off 三态：auto 先记 None，运行中探测决定。"""
        text = str(value or "auto").strip().lower()
        if text in ("on", "1", "true", "yes"):
            return True
        if text in ("off", "0", "false", "no"):
            return False
        return None

    def _progress(self, kind, **payload):
        try:
            self.on_progress(kind, payload)
        except Exception:
            pass

    def _call(self, func, *args, **kwargs):
        """接口调用包装：401 时重登一次。"""
        try:
            return func(*args, **kwargs)
        except netio.Unauthorized:
            self.logger.warn("会话失效，尝试重新登录……")
            if not self.relogin or not self.relogin():
                raise
            return func(*args, **kwargs)

    def _wrong_items(self, force=False):
        now = time.monotonic()
        if not force and self._wrong_cache is not None and (now - self._wrong_ts) < 0.4:
            return self._wrong_cache
        items = self._call(self.api.wrong)
        self._wrong_cache = items
        self._wrong_ts = now
        return items

    # ------------------------------------------------------------- 主流程
    def run(self):
        started = time.time()
        paper = self.paper
        paper_name = paper.get("name") or ""
        mode = self.cfg.get("mode") or "exam"
        total_declared = int(paper.get("total") or 0)
        points = self._decide_points(paper)

        self._progress("starting", paper=paper_name, paper_id=self.paper_id)
        self.logger.info("开始作答：%s（%s，共 %s 题，满分 %s）" %
                         (paper_name, "模考" if mode == "exam" else "练习",
                          total_declared, paper.get("max_score")))
        if points is True:
            self.logger.info("已勾选：把本次成绩计入个人积分")
        elif points is False:
            self.logger.info("本次不计入积分（积分次数已满或已关闭）")

        data = self._call(self.api.start_attempt, self.paper_id, mode,
                          bool(self.cfg.get("fresh")), points)
        attempt = (data or {}).get("attempt") or {}
        self.attempt_id = int(attempt.get("id") or 0)
        if not self.attempt_id:
            raise AttemptFailed("服务端没有返回 attempt.id，接口可能变了")
        self.paper_name = attempt.get("paper_name") or paper_name
        self.slots = apimod.flatten_questions((data or {}).get("questions"))
        if not self.slots:
            raise AttemptFailed("这套试卷没有题目")
        self.logger.info("attempt=%d，实际 %d 题，服务端已用 %s 秒%s" % (
            self.attempt_id, len(self.slots),
            int((data or {}).get("elapsed") or 0),
            "（恢复了上次进度）" if (data or {}).get("resumed") else ""))
        self._progress("running", attempt_id=self.attempt_id,
                       total=len(self.slots), done=0)

        self._start_beat()
        try:
            strategy = self.cfg.get("strategy") or "per-question"
            if strategy == "per-question" and self._deadline_risk(len(self.slots)):
                self.logger.warn("按当前延时估算会超时，自动切换为 batch 策略")
                strategy = "batch"
            if strategy == "batch":
                self._run_batch()
            else:
                self._run_per_question()
            if self.cfg.get("final_check", True):
                self._final_check()
            else:
                self._flush_unresolved(self._probe_records, "未做交卷前复核")
        finally:
            self._stop_beat()

        summary = None
        if self.cfg.get("submit", True):
            summary = self._submit()
        else:
            self.logger.warn("已按要求跳过交卷；下次可用 fresh=false 继续作答")
            self._progress("done", submitted=False)

        result = {
            "account": self.username,
            "paper": self.paper_name,
            "paper_id": self.paper_id,
            "attempt_id": self.attempt_id,
            "total": len(self.slots),
            "counts": dict(self.counts),
            "unresolved": list(self.unresolved),
            "summary": summary,
            "elapsed": round(time.time() - started, 1),
            "submitted": summary is not None,
        }
        self._progress("done", submitted=summary is not None, result=result)
        return result

    # --------------------------------------------------------- 逐题（默认）
    def _run_per_question(self):
        total = len(self.slots)
        for index, slot in enumerate(self.slots):
            self._index = index
            t0 = time.monotonic()
            label = "[%2d/%2d]" % (index + 1, total)
            try:
                outcome = self._solve_one(slot, label)
            except (AttemptFailed,) as exc:
                self.counts["failed"] += 1
                self.logger.error("%s 跳过：%s" % (label, exc))
                outcome = {"source": "failed"}
            spent = time.monotonic() - t0
            target = self._target_delay()
            if spent < target:
                time.sleep(target - spent)
            spent = time.monotonic() - t0
            self.logger.info("%s %s" % (label, self._describe(slot, outcome, spent)))
            self._progress("progress", attempt_id=self.attempt_id, total=total,
                           done=index + 1, counts=dict(self.counts))
    # ----------------------------------------------------------- batch
    def _run_batch(self):
        total = len(self.slots)
        self.logger.info("batch 策略：先对所有 %d 题提交探针答案" % total)
        probes = {}
        for index, slot in enumerate(self.slots):
            self._index = index
            question = slot["q"]
            answer = self._fast_answer(slot)
            if answer is not None:
                self.counts[self._answer_source(slot)] += 1
                probes[int(question["id"])] = {"answer": answer, "known": True}
                self.logger.info("[%2d/%2d] 已知答案 → %s" % (index + 1, total, answer))
                continue
            value = self._probe_value(question)
            try:
                self._send(question, value)
                probes[int(question["id"])] = {"answer": value, "known": False}
                self.logger.info("[%2d/%2d] 探针 → %s" % (index + 1, total, value))
            except Exception as exc:
                self.counts["failed"] += 1
                self.logger.error("[%2d/%2d] 探针失败：%s" % (index + 1, total, exc))
            self._progress("progress", attempt_id=self.attempt_id, total=total,
                           done=index + 1, counts=dict(self.counts))
            time.sleep(random.uniform(0.05, 0.2))
        # 一次拉错题本批量回填
        self.logger.info("批量查询错题本……")
        time.sleep(float(self.cfg.get("poll_interval") or 0.8))
        index = matcher.WrongBookIndex(self._wrong_items(force=True))
        fixed = 0
        for slot in self.slots:
            question = slot["q"]
            if probes.get(int(question["id"]), {}).get("known"):
                continue
            result = index.find(self.paper_name, question.get("stem"))
            if result["status"] == "ambiguous":
                self.counts["ambiguous"] += 1
                continue
            if result["status"] != "found":
                self.counts["unknown"] += 1
                self._probe_records[int(question["id"])] = {
                    "question": question,
                    "probe": probes.get(int(question["id"]), {}).get("answer"),
                }
                continue
            if not matcher.answer_is_sane(question, result["answer"]):
                self.counts["ambiguous"] += 1
                continue
            try:
                self._send(question, result["answer"])
                self.counts["wrongbook"] += 1
                fixed += 1
                self._remember(question, result["answer"], "wrongbook")
            except Exception as exc:
                self.logger.error("回填失败 qid=%s：%s" % (question["id"], exc))
        self.logger.ok("batch 完成：回填 %d 题，未解决 %d 题" % (fixed, len(self.unresolved)))

    # --------------------------------------------------------- 单题处理
    def _solve_one(self, slot, label):
        question = slot["q"]
        question_id = int(question["id"])

        answer = self._fast_answer(slot)
        if answer is not None:
            source = self._answer_source(slot)
            self._send(question, answer)
            self.counts[source] += 1
            return {"source": source, "answer": answer, "probe": ""}

        probe = self._probe_value(question)
        reply = self._send(question, probe)

        # 捷径 A：/answer 直接给了正确答案
        if self.trust_answer_api is None:
            correct = str((reply or {}).get("correct_answer") or "").strip()
            if correct:
                self.trust_answer_api = True
                self.logger.ok("发现 /answer 会返回正确答案，后续走捷径")
        if self.trust_answer_api:
            correct = str((reply or {}).get("correct_answer") or "").strip()
            if correct and matcher.answer_is_sane(question, correct):
                self._send(question, correct)
                self.counts["answer_api"] += 1
                self._remember(question, correct, "answer_api")
                return {"source": "answer_api", "answer": correct, "probe": probe}

        # 主路径：查错题本
        lookup = self._lookup(question)
        if lookup and lookup["status"] == "found":
            self._send(question, lookup["answer"])
            self.counts["wrongbook"] += 1
            self._remember(question, lookup["answer"], "wrongbook")
            return {"source": "wrongbook", "answer": lookup["answer"], "probe": probe}
        if lookup and lookup["status"] == "ambiguous":
            self.counts["ambiguous"] += 1
            self.unresolved.append({
                "question_id": question_id,
                "seq": question.get("seq"),
                "type": question.get("type"),
                "stem": matcher.stem_preview(question.get("stem"), 50),
                "reason": "错题本里同一题干有多个不同答案",
            })
            return {"source": "ambiguous", "answer": probe, "probe": probe}

        self.counts["unknown"] += 1
        self._probe_records[question_id] = {"question": question, "probe": probe}
        return {"source": "unknown", "answer": probe, "probe": probe}

    def _fast_answer(self, slot):
        """题目自带答案 / 缓存，能直接答对就不用探针。"""
        question = slot["q"]
        if self.trust_questions is None:
            if str(question.get("answer") or "").strip():
                self.trust_questions = True
                self.logger.ok("注意：题目接口直接带 answer 字段，后续直接答对（最快路径）")
            else:
                self.trust_questions = False
        if self.trust_questions:
            answer = str(question.get("answer") or "").strip()
            if answer and matcher.answer_is_sane(question, answer):
                return answer
        if self.cache is not None:
            entry = self.cache.get(self.paper_id, question.get("stem"))
            if entry and matcher.answer_is_sane(question, entry.get("answer")):
                return str(entry.get("answer")).strip()
        return None

    def _answer_source(self, slot):
        question = slot["q"]
        if self.trust_questions and str(question.get("answer") or "").strip():
            return "questions"
        return "cache"

    def _probe_value(self, question):
        keys = apimod.option_keys(question)
        if question.get("type") == "multi" and len(keys) >= 2:
            count = random.randint(1, min(2, len(keys)))
            return "".join(sorted(random.sample(keys, count)))
        if keys:
            return random.choice(keys)
        return str(self.cfg.get("probe_fill") or "X")

    def _lookup(self, question):
        """轮询错题本；未命中会重试几次确认。"""
        interval = float(self.cfg.get("poll_interval") or 0.8)
        tries = max(1, int(self.cfg.get("poll_tries") or 3))
        last = {"status": "missing", "answer": "", "item": None, "via": ""}
        for attempt in range(tries):
            index = matcher.WrongBookIndex(self._wrong_items(force=True))
            result = index.find(self.paper_name, question.get("stem"))
            if result["status"] == "found":
                if matcher.answer_is_sane(question, result["answer"]):
                    if attempt:
                        self.logger.debug("错题本第 %d 次轮询才命中" % (attempt + 1))
                    return result
                return {"status": "ambiguous", "answer": "",
                        "item": result["item"], "via": result["via"]}
            if result["status"] == "ambiguous":
                return result
            last = result
            if attempt < tries - 1:
                time.sleep(interval * random.uniform(0.85, 1.25))
        return last

    def _send(self, question, value):
        return self._call(self.api.answer, self.attempt_id, int(question["id"]), value)

    def _remember(self, question, answer, source):
        if self.cache is not None:
            self.cache.put(self.paper_id, self.paper_name, question.get("stem"), answer, source)

    def _target_delay(self):
        low = float(self.cfg.get("delay_min") or 0.0)
        high = float(self.cfg.get("delay_max") or low)
        if high < low:
            high = low
        if high <= 0:
            return 0.0
        return random.uniform(low, high)

    def _deadline_risk(self, total):
        limit = float(self.cfg.get("assume_limit") or 0)
        if limit <= 0:
            return False
        high = float(self.cfg.get("delay_max") or 0)
        return total * high > limit * 0.8

    # ----------------------------------------------------------- 预取/复核
    def _final_check(self):
        """交卷前再拉一次错题本做最终裁定。

        此时还在册的题 = 我们探针答错且没回填 → 补答正确答案；
        此时不在册的题 = 探针那次其实答对了 → 把它记进缓存（下次直接答对）。
        做完这一步，"未命中"才敢当作"已答对"。
        """
        pending = self._probe_records
        if not pending:
            return
        try:
            items = self._wrong_items(force=True)
        except Exception as exc:
            self.logger.warn("交卷前复核跳过（取错题本失败：%s）" % exc)
            self._flush_unresolved(pending, "交卷前复核失败，答案未确认")
            return
        index = matcher.WrongBookIndex(items)
        fixed = 0
        confirmed = 0
        for question_id in list(pending.keys()):
            record = pending[question_id]
            question = record.get("question") or {}
            result = index.find(self.paper_name, question.get("stem"))
            if result["status"] == "found" and matcher.answer_is_sane(question, result["answer"]):
                try:
                    self._send(question, result["answer"])
                    fixed += 1
                    self.counts["wrongbook"] += 1
                    self._remember(question, result["answer"], "wrongbook-final")
                    pending.pop(question_id, None)
                except Exception as exc:
                    self.logger.warn("复核补答失败 qid=%s：%s" % (question_id, exc))
                continue
            probe = str(record.get("probe") or "").strip()
            if probe:
                self._remember(question, probe, "probe-ok")
                confirmed += 1
            pending.pop(question_id, None)
        if fixed:
            self.logger.ok("交卷前复核：补答 %d 题" % fixed)
        if confirmed:
            self.logger.info("交卷前复核：确认 %d 题的探针答案就是正确答案" % confirmed)
        if pending:
            self._flush_unresolved(pending, "交卷前复核仍没在错题本里找到")

    def _flush_unresolved(self, records, reason):
        for question_id in list(records.keys()):
            record = records.pop(question_id)
            question = record.get("question") or {}
            self.unresolved.append({
                "question_id": question_id,
                "seq": question.get("seq"),
                "type": question.get("type"),
                "stem": matcher.stem_preview(question.get("stem"), 50),
                "reason": reason,
            })

    # --------------------------------------------------------------- 心跳
    def _start_beat(self):
        interval = 15.0
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.api.beat(self.attempt_id, self._index)
                except Exception:
                    pass

        self._beat_thread = threading.Thread(target=loop, name="beat")
        self._beat_thread.daemon = True
        self._beat_thread.start()

    def _stop_beat(self):
        self._stop.set()
        thread = self._beat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    # --------------------------------------------------------------- 交卷
    def _submit(self):
        self.logger.info("交卷……")
        reply = None
        for attempt in range(1, 6):
            try:
                reply = self._call(self.api.submit, self.attempt_id)
                break
            except netio.NetworkError as exc:
                self.logger.warn("交卷失败（第 %d 次）：%s" % (attempt, exc))
                time.sleep(2.0 * attempt)
        if not reply:
            self.logger.error("交卷没有成功，请到网页上手工交卷")
            return None
        summary = (reply or {}).get("summary") or {}
        self._print_summary(summary)
        return summary

    def _print_summary(self, summary):
        def num(key, default=0):
            try:
                return float(summary.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        self.logger.plain("")
        self.logger.plain("-" * 62)
        self.logger.plain("  本次成绩：%s / %s" % (_trim(num("score")), _trim(num("max_score"))))
        self.logger.plain("  答对 %s 题 · 作答 %s 题 · 正确率 %s%% · 用时 %s" % (
            _trim(num("correct")), _trim(num("answered")), _trim(num("rate")),
            _fmt_duration(num("duration") or num("elapsed"))))
        awarded = num("points_awarded")
        if summary.get("points_enabled"):
            if awarded > 0:
                self.logger.plain("  个人积分 +%s（本卷已计 %s/3 次）" %
                                  (_trim(awarded), _trim(num("points_count"))))
            else:
                self.logger.plain("  积分：%s" % (summary.get("points_message") or "本次未计入"))
        self.logger.plain("-" * 62)
        self.logger.plain("")
        self._progress("summary", summary=summary)

    # ------------------------------------------------------------- 文本化
    def _describe(self, slot, outcome, spent):
        question = slot["q"]
        label = apimod.question_label(question)
        source = outcome.get("source")
        answer = outcome.get("answer")
        probe = outcome.get("probe")
        if source == "questions":
            body = "题目自带答案 → %s" % _show(answer)
        elif source == "cache":
            body = "缓存命中 → %s" % _show(answer)
        elif source == "answer_api":
            body = "探针=%s → 接口直接给答案 → %s" % (_show(probe), _show(answer))
        elif source == "wrongbook":
            body = "探针=%s → 错题本命中 → 回填%s" % (_show(probe), _show(answer))
        elif source == "ambiguous":
            body = "探针=%s → 错题本答案冲突，保留探针" % _show(probe)
        elif source == "failed":
            body = "跳过（调用失败）"
        else:
            body = "探针=%s → 错题本未收录（视为已答对）" % _show(probe)
        return "%s %s → 用时 %.1fs" % (label, body, spent)

    def _decide_points(self, paper):
        """完全复刻 student.js:107,141-142 的弹窗逻辑。"""
        enabled = bool(paper.get("points_enabled")) or bool(paper.get("formal_exam"))
        if not enabled:
            return None
        awards = int(paper.get("points_awards") or 0)
        if paper.get("formal_exam"):
            wanted = awards < 3
        else:
            wanted = bool(self.cfg.get("points_opt_in")) and awards < 3
        return bool(wanted)


def _show(value):
    text = str(value or "").strip()
    if not text:
        return "（空）"
    if len(text) > 24:
        text = text[:23] + "…"
    return text


def _trim(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 0.01:
        return str(int(round(number)))
    return ("%.1f" % number)


def _fmt_duration(seconds):
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        total = 0
    hours, rest = divmod(max(0, total), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)
