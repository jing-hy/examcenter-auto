# -*- coding: utf-8 -*-
"""工作进程：一个进程 = 一个账号 = 一个控制台窗口。

单独运行（账号从命令行给，不落盘）：
    python -m autoexam.worker --account 学号 --paper 2
    python -m autoexam.worker --job status/job-acc-3f2a91c4.json

隐私约定（重要）：
* 账号 / 姓名只在内存与控制台出现，日志文件名与状态文件名一律用
  privacy.slug() 生成的匿名 id；
* 写日志文件前会把账号、姓名、密码替换成 ***；
* 任务文件里没有账号和密码，子进程通过环境变量 AUTOEXAM_ACCOUNT /
  AUTOEXAM_PASSWORD 拿到它们。
"""

import io
import json
import os
import sys
import time

from . import api as apimod
from . import config as configmod
from . import login as loginmod
from . import matcher
from . import netio
from . import privacy
from . import solver as solvermod
from . import ui


def _account_slug(name):
    return privacy.slug(name)


class AccountLock(object):
    """同账号重复启动的护栏（超时自动视为陈旧锁）。"""

    STALE_SECONDS = 3 * 3600

    def __init__(self, path):
        self.path = path
        self.acquired = False

    def acquire(self):
        try:
            if os.path.isfile(self.path):
                age = time.time() - os.path.getmtime(self.path)
                if age < self.STALE_SECONDS:
                    with io.open(self.path, "r", encoding="utf-8") as fh:
                        info = fh.read().strip()
                    return False, info or "另一个进程"
                os.remove(self.path)
        except OSError:
            pass
        try:
            with io.open(self.path, "w", encoding="utf-8") as fh:
                fh.write("pid=%s time=%s" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
            self.acquired = True
            return True, ""
        except Exception as exc:
            return True, "锁文件写不了（%s），继续运行" % exc

    def release(self):
        if not self.acquired:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass


def _is_plan(paper):
    return isinstance(paper, dict) and paper.get("_kind") == "points-plan"


def _rounds(paper):
    """把"一套卷"或"全部积分计划"统一成 [(paper, times, guard)]。

    guard=True 表示每跑一遍之前都要重新确认积分次数，用光就停。
    """
    if _is_plan(paper):
        rounds = []
        for task in paper.get("tasks") or []:
            item = task.get("paper") or {}
            try:
                times = int(task.get("times") or 1)
            except (TypeError, ValueError):
                times = 1
            rounds.append((item, max(1, times), True))
        return rounds
    return [(paper or {}, 1, False)]


def _refresh_paper(api, paper, guard, logger):
    """重新拉一次试卷列表，拿到最新的 points_awards。"""
    if not guard:
        return paper
    paper_id = int(paper.get("id") or 0)
    if not paper_id:
        return paper
    try:
        latest = api.find_paper(paper_id)
    except Exception as exc:
        logger.warn("刷新试卷信息失败（按原计划继续）：%s" % exc)
        return paper
    if not latest:
        return paper
    left = apimod.points_remaining(latest)
    if left <= 0:
        logger.ok("《%s》积分次数已满，跳过。" % (latest.get("name") or paper_id))
        return None
    return latest


def _merge_results(results, account_slug, plan_name=""):
    """把多轮结果合成一份 result-*.json。"""
    merged_summary = {
        "score": 0.0,
        "max_score": 0.0,
        "correct": 0.0,
        "answered": 0.0,
        "rate": 0.0,
        "duration": 0.0,
        "points_awarded": 0.0,
        "points_count": 0.0,
        "points_enabled": False,
        "points_message": "",
    }
    counts = {}
    unresolved = []
    rounds = []
    papers = []
    # points_count 是"这套卷累计计了几次"，多轮之间不能相加，只取每套卷的末值
    per_paper_points = {}
    for item in results:
        summary = item.get("summary") or {}
        for key in ("score", "max_score", "correct", "answered", "duration",
                    "points_awarded"):
            try:
                merged_summary[key] += float(summary.get(key) or 0)
            except (TypeError, ValueError):
                pass
        try:
            per_paper_points[item.get("paper_id")] = int(summary.get("points_count") or 0)
        except (TypeError, ValueError):
            pass
        if summary.get("points_enabled"):
            merged_summary["points_enabled"] = True
        for key, value in (item.get("counts") or {}).items():
            try:
                counts[key] = counts.get(key, 0) + int(value or 0)
            except (TypeError, ValueError):
                pass
        unresolved.extend(item.get("unresolved") or [])
        rounds.append({
            "paper": item.get("paper"),
            "paper_id": item.get("paper_id"),
            "attempt_id": item.get("attempt_id"),
            "submitted": item.get("submitted"),
            "elapsed": item.get("elapsed"),
            "summary": summary,
        })
        name = item.get("paper")
        if name and name not in papers:
            papers.append(name)
    merged_summary["points_count"] = float(sum(per_paper_points.values()))
    answered = merged_summary["answered"]
    if answered:
        merged_summary["rate"] = round(merged_summary["correct"] * 100.0 / answered, 1)
    if len(papers) > 1 or len(rounds) > 1:
        merged_summary["points_message"] = "全部积分：%d 轮作答" % len(rounds)
    return {
        "account": account_slug,
        "paper": plan_name or (papers[0] if papers else ""),
        "papers": papers,
        "rounds": rounds,
        "counts": counts,
        "unresolved": unresolved,
        "summary": merged_summary if len(rounds) > 1 else (results[0].get("summary") if results else None),
        "elapsed": round(sum(float(item.get("elapsed") or 0) for item in results), 1),
        "submitted": all(item.get("submitted") for item in results) if results else False,
    }


def run(cfg, username, password, paper, job=None):
    """跑完一个账号（可能包含多套卷 / 多次机会），返回结果 dict。"""
    slug = _account_slug(username)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_dir = cfg.get("log_dir") or "logs"
    log_path = os.path.join(log_dir, "%s-%s.log" % (slug, stamp))
    logger = ui.Logger(
        log_path,
        prefix=username,
        file_prefix=privacy.mask_account(username),
        secrets=[username, password],
    )
    status_dir = cfg.get("status_dir") or "status"
    status_path = os.path.join(status_dir, "%s.json" % slug)
    result_path = os.path.join(status_dir, "result-%s.json" % slug)

    plan_name = paper.get("name") if _is_plan(paper) else ""
    state = {
        "account": slug,
        "phase": "init",
        "paper": plan_name or (paper or {}).get("name") or "",
        "paper_id": (paper or {}).get("id"),
        "done": 0,
        "total": int((paper or {}).get("total") or 0),
        "counts": {},
        "summary": None,
        "log": log_path,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
    }
    if job:
        state["job"] = privacy.redact_text(str(job), [username, password])

    def flush(**changes):
        state.update(changes)
        state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ui.write_status(status_path, state)

    def on_progress(kind, payload):
        if kind == "progress":
            flush(phase="running", done=payload.get("done") or 0,
                  total=payload.get("total") or state.get("total") or 0,
                  counts=payload.get("counts") or {})
        elif kind == "starting":
            flush(phase="starting")
        elif kind == "summary":
            flush(phase="submitted", summary=payload.get("summary"))
        elif kind == "done":
            flush(phase="finished", summary=(payload.get("result") or {}).get("summary"))

    flush(phase="starting")

    client = netio.HttpClient(
        cfg.get("base_url"),
        timeout=cfg.get("http_timeout") or 15,
        retries=cfg.get("retries") or 3,
        save_raw=bool(cfg.get("save_raw")),
        raw_dir=cfg.get("raw_dir") or "logs/raw",
        logger=logger,
        tag_prefix=slug,
        insecure_tls=bool(cfg.get("insecure_tls")),
        proxy=cfg.get("proxy") or None,
    )
    api = apimod.ExamCenter(client, logger)

    if cfg.get("cookie"):
        client.import_cookies(cfg["cookie"])

    user = loginmod.login(cfg, client, logger, username, password)
    # 登录之后才知道姓名，登记进脱敏列表，之后的落盘内容里不会再出现
    logger.add_secret(user.get("name"))
    flush(phase="logged-in")

    if not paper:
        paper = api.find_paper(cfg.get("paper")) or {}
        if not paper:
            raise solvermod.AttemptFailed("找不到试卷 id=%s" % cfg.get("paper"))
        plan_name = ""
        state["paper"] = paper.get("name") or ""
        state["paper_id"] = paper.get("id")
        state["total"] = int(paper.get("total") or 0)

    cache = matcher.AnswerCache(cfg.get("cache_path"), bool(cfg.get("use_cache", True)), logger)

    def relogin():
        try:
            loginmod.login(cfg, client, logger, username, password)
            return True
        except Exception as exc:
            logger.error("重新登录失败：%s" % exc)
            return False

    rounds = _rounds(paper)
    logger.info("日志文件：%s" % log_path)
    logger.info("答案缓存：%s（已有 %d 条）" % (cfg.get("cache_path"), cache.size()))
    if len(rounds) > 1 or _is_plan(paper):
        logger.info("全部积分模式：%d 套卷，共 %d 次作答" % (
            len(rounds), sum(item[1] for item in rounds)))

    results = []
    for round_index, (round_paper, times, guard) in enumerate(rounds):
        done_times = 0
        for sub_index in range(times):
            current = _refresh_paper(api, round_paper, guard, logger)
            if current is None:
                break
            round_paper = current
            label = "第 %d/%d 套 %s" % (round_index + 1, len(rounds), round_paper.get("name"))
            if times > 1:
                label += " 第 %d/%d 次" % (sub_index + 1, times)
            logger.plain("")
            logger.info("开始：%s（已计积分 %s/3）" % (
                label, round_paper.get("points_awards") or 0))
            flush(phase="running", paper=round_paper.get("name") or "",
                  paper_id=round_paper.get("id"),
                  total=int(round_paper.get("total") or 0), done=0, round=round_index + 1)
            engine = solvermod.Solver(
                cfg, api, logger, username, round_paper, cache,
                on_progress=on_progress, relogin=relogin,
            )
            try:
                result = engine.run()
            except netio.NetworkError as exc:
                logger.error("网络中断，本次中止：%s" % exc)
                flush(phase="error", error=str(exc))
                raise
            except netio.Unauthorized as exc:
                logger.error("登录态失效，本次中止：%s" % exc)
                flush(phase="error", error=str(exc))
                raise
            except solvermod.AttemptFailed as exc:
                logger.error("无法继续：%s" % exc)
                flush(phase="error", error=str(exc))
                raise
            results.append(result)
            done_times += 1
            flush(phase="round-done", round=round_index + 1, round_done=done_times)

    if not results:
        raise solvermod.AttemptFailed("一次都没跑起来（所有卷的积分次数都已用完？）")

    merged = _merge_results(results, slug, plan_name)
    merged["log"] = log_path
    ui.write_status(result_path, merged)
    flush(phase="finished", summary=merged.get("summary"), counts=merged.get("counts"))
    summary = merged.get("summary") or {}
    logger.ok("完成：%d 轮 · 合计 %s/%s 分 · 积分 +%s · 用时 %s 秒" % (
        len(results), _num(summary.get("score")), _num(summary.get("max_score")),
        _num(summary.get("points_awarded")), merged.get("elapsed")))
    logger.ok("结果文件：%s" % result_path)
    if merged.get("unresolved"):
        logger.warn("有 %d 题没拿到标准答案，已记入结果文件" % len(merged["unresolved"]))
    return merged


def _num(value):
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def main(argv=None):
    cfg, args = configmod.cli_config(argv)
    job = {}
    if getattr(args, "job", None) and os.path.isfile(args.job):
        try:
            with io.open(args.job, "r", encoding="utf-8") as fh:
                job = json.load(fh)
        except Exception as exc:
            ui.safe_print("读 job 文件失败：%s" % exc)
            return 2
        overrides = dict(job)
        # 主进程已经把所有决策（含命令行参数）写进 job 文件，这里整体覆盖，
        # 避免 worker 重新读 config.json 时丢掉 --delay-min 这类临时参数。
        for key in configmod.DEFAULTS:
            if key in ("accounts", "paper"):
                continue
            if key in overrides:
                cfg[key] = overrides[key]
        # 目录类不在 DEFAULTS 里，但必须跟随主进程（见 supervisor.job_payload）
        for key in ("log_dir", "raw_dir", "status_dir", "cache_path"):
            if overrides.get(key):
                cfg[key] = overrides[key]

    # 账号与密码：环境变量优先（主进程通过 env 传，不落盘）
    username = (os.environ.get("AUTOEXAM_ACCOUNT") or job.get("account")
                or cfg.get("account") or "")
    paper = job.get("paper")
    if not username:
        ui.safe_print("缺少账号：主进程会用环境变量 AUTOEXAM_ACCOUNT 传递；")
        ui.safe_print("手工运行时请加 --account 学号。")
        return 2
    if not cfg.get("base_url"):
        ui.safe_print("缺少服务器地址（--base 或工作区里的网址 txt）")
        return 2

    password = (os.environ.get("AUTOEXAM_PASSWORD") or job.get("password")
                or cfg.get("password") or "123456")
    lock = AccountLock(os.path.join(cfg.get("status_dir") or "status",
                                    "%s.lock" % _account_slug(username)))
    acquired, info = lock.acquire()
    if not acquired:
        ui.safe_print("账号 %s 看起来已经有实例在跑（%s）。" % (privacy.mask_account(username), info))
        ui.safe_print("如确认没有，请删掉 status/ 下对应的 .lock 文件后重试。")
        return 3

    try:
        run(cfg, username, password, paper, job=args.job)
    except Exception as exc:
        ui.safe_print("")
        ui.safe_print("[错误] %s: %s" % (type(exc).__name__, exc))
        code = 1
    else:
        code = 0
    finally:
        lock.release()

    if cfg.get("input_wait", True) and sys.stdin and sys.stdin.isatty():
        try:
            ui.safe_print("")
            ui.safe_print("按回车关闭窗口……")
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
