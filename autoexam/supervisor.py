# -*- coding: utf-8 -*-
"""主进程：登录 → 列试卷 → 选卷 → 收集账号 → 每个账号拉起一个独立控制台。

按你的要求做成"一账号一进程一窗口"：主进程只负责准备与汇总，真正刷题的工作
在 worker.py 里跑。这样某个账号崩了不会牵连其他账号，也方便单独重跑。
"""

import io
import json
import os
import subprocess
import sys
import time

from . import api as apimod
from . import config as configmod
from . import login as loginmod
from . import netio
from . import privacy
from . import ui
from . import worker as workermod

CREATE_NEW_CONSOLE = 0x00000010


def resolve_base(cfg, logger):
    if cfg.get("base_url"):
        return cfg["base_url"]
    logger.error("没有服务器地址。请把刷题网址放在工作区的 txt 里，或用 --base 指定。")
    return ""


def make_client(cfg, logger, tag_prefix=""):
    client = netio.HttpClient(
        cfg.get("base_url"),
        timeout=cfg.get("http_timeout") or 15,
        retries=cfg.get("retries") or 3,
        save_raw=bool(cfg.get("save_raw")),
        raw_dir=cfg.get("raw_dir") or "logs/raw",
        logger=logger,
        tag_prefix=tag_prefix,
        insecure_tls=bool(cfg.get("insecure_tls")),
        proxy=cfg.get("proxy") or None,
    )
    if cfg.get("cookie"):
        client.import_cookies(cfg["cookie"])
    return client


def probe_login(cfg, logger, username, password):
    """用给定账号登录，返回 (client, api, user)。"""
    client = make_client(cfg, logger, tag_prefix="probe")
    api = apimod.ExamCenter(client, logger)
    user = loginmod.login(cfg, client, logger, username, password)
    return client, api, user


def show_papers(papers, logger):
    logger.plain("")
    logger.plain("-" * 72)
    logger.plain("  可选试卷：")
    for index, paper in enumerate(papers):
        logger.plain("  " + apimod.paper_line(paper, index + 1))
    logger.plain("-" * 72)
    logger.plain("")


def pick_paper(api, logger, cfg):
    """列出试卷并让用户选一套。

    --paper 指定时直接取；--all-points 或菜单里输入 A 时返回一份"全部积分"计划。
    """
    try:
        papers = api.papers()
    except (netio.ApiError, netio.NetworkError) as exc:
        logger.error("取试卷列表失败：%s" % exc)
        return None
    if not papers:
        logger.error("试卷列表是空的。")
        return None

    allow_formal = bool(cfg.get("allow_formal"))
    selectable = []
    for paper in papers:
        if paper.get("formal_exam") and not allow_formal:
            logger.warn("跳过正式考试卷：%s（需要时加 --allow-formal）" % paper.get("name"))
            continue
        selectable.append(paper)
    if not selectable:
        logger.error("没有可刷的试卷。")
        return None

    wanted = cfg.get("paper")
    if wanted is not None:
        for paper in selectable:
            if int(paper.get("id") or 0) == int(wanted):
                logger.info("已指定试卷：%s" % paper.get("name"))
                return _check_open(paper, logger)
        logger.error("没有找到 id=%s 的试卷。" % wanted)
        return None

    if cfg.get("all_points"):
        return build_points_plan(selectable, logger, allow_formal=allow_formal)

    show_papers(selectable, logger)
    while True:
        raw = ui.ask("  请输入试卷序号（1-%d），A=全部积分，0=退出：" % len(selectable), default="")
        text = (raw or "").strip()
        if text.lower() in ("a", "all", "全部", "全部积分"):
            plan = build_points_plan(selectable, logger, allow_formal=allow_formal)
            if plan is None:
                continue
            for line in describe_plan(plan):
                ui.safe_print("  " + line)
            if ui.ask_yes_no("  按「全部积分」开始，对吗？", default=True):
                return plan
            continue
        try:
            index = int(text)
        except (TypeError, ValueError):
            ui.safe_print("  请输入数字，或输入 A 表示全部积分。")
            continue
        if index == 0:
            return None
        if index < 0 or index > len(selectable):
            ui.safe_print("  没有这个序号。")
            continue
        paper = selectable[index - 1]
        ready = _check_open(paper, logger)
        if ready is None:
            continue
        if ui.ask_yes_no("  就用《%s》开始，对吗？" % paper.get("name"), default=True):
            return ready


def build_points_plan(papers, logger, allow_formal=False):
    """把"所有还能计分的卷 + 各自剩余次数"编成一张计划表。

    返回的 dict 会一路传给 worker：worker 按顺序把每套卷跑 times 遍，
    每一遍开始前再重新查一次 points_awards，次数用光就跳过。
    """
    tasks = []
    for paper in papers:
        left = apimod.points_remaining(paper)
        if left <= 0:
            continue
        if paper.get("formal_exam") and not allow_formal:
            logger.warn("跳过正式考试卷：%s（需要时加 --allow-formal）" % paper.get("name"))
            continue
        if paper.get("exam_open") in (0, False) and not paper.get("formal_exam"):
            logger.warn("跳过（模考入口关闭）：%s" % paper.get("name"))
            continue
        tasks.append({"paper": paper, "times": left})
    if not tasks:
        logger.warn("没有还能计分的试卷：要么积分次数已满，要么这些卷没开积分。")
        ui.safe_print("")
        ui.safe_print("  提示：积分次数按卷统计，每卷最多计 3 次；")
        ui.safe_print("        要看每卷剩余几次，回到试卷列表看「还可计分」那一行。")
        return None
    total_times = sum(item["times"] for item in tasks)
    return {
        "_kind": "points-plan",
        "name": "全部积分（%d 套卷 / %d 次）" % (len(tasks), total_times),
        "id": 0,
        "total": 0,
        "tasks": tasks,
    }


def describe_plan(plan):
    """把计划表变成给人看的几行字（控制台用，不写日志）。"""
    lines = []
    tasks = plan.get("tasks") or []
    lines.append("全部积分模式：共 %d 套卷、%d 次作答" % (
        len(tasks), sum(int(item.get("times") or 0) for item in tasks)))
    for index, item in enumerate(tasks):
        paper = item.get("paper") or {}
        lines.append("  %2d. %s  id=%s  刷 %d 次（已计 %s/3）" % (
            index + 1, paper.get("name"), paper.get("id"), int(item.get("times") or 0),
            paper.get("points_awards") or 0))
    return lines


def is_plan(paper):
    return isinstance(paper, dict) and paper.get("_kind") == "points-plan"


def _check_open(paper, logger):
    mode = "exam"
    if paper.get("formal_exam"):
        logger.warn("这是正式考试卷，成绩会真实计入系统。")
    open_flag = paper.get("exam_open")
    if open_flag in (0, False) and not paper.get("formal_exam"):
        logger.warn("这套卷的『模拟考试』入口是关闭的（exam_open=0）。")
        if not ui.ask_yes_no("  仍然尝试强行进入吗？", default=False):
            return None
    return paper


def collect_accounts(cfg, default_user):
    """确定要跑哪些账号。"""
    accounts = []
    for item in cfg.get("accounts") or []:
        if isinstance(item, dict) and item.get("username"):
            accounts.append({
                "username": item["username"],
                "password": item.get("password") or cfg.get("password") or "123456",
            })
    if accounts:
        return accounts

    file_accounts = configmod.load_accounts_file(
        os.path.join(cfg.get("root") or ".", "accounts.txt"))
    if file_accounts:
        ui.safe_print("")
        ui.safe_print("  发现 accounts.txt，里面有 %d 个账号：" % len(file_accounts))
        for item in file_accounts[:20]:
            ui.safe_print("    - %s" % item["username"])
        if len(file_accounts) > 20:
            ui.safe_print("    ……等共 %d 个" % len(file_accounts))
        if ui.ask_yes_no("  使用这份清单吗？", default=True):
            for item in file_accounts:
                accounts.append({
                    "username": item["username"],
                    "password": item.get("password") or cfg.get("password") or "123456",
                })
            return accounts

    ui.safe_print("")
    ui.safe_print("  要刷哪些账号？多个用逗号或空格分隔，支持 账号:密码 覆盖密码。")
    ui.safe_print("  直接回车 = 只用刚才那个账号（%s）。" % default_user)
    raw = ui.ask("  账号：", default="")
    if not raw:
        return [{"username": default_user, "password": cfg.get("password") or "123456"}]
    accounts = []
    for item in configmod.split_accounts(raw):
        accounts.append({
            "username": item["username"],
            "password": item.get("password") or cfg.get("password") or "123456",
        })
    return accounts


def job_payload(cfg, account, paper):
    """任务文件内容：只放匿名 id 与配置，账号与密码走环境变量。

    这个文件会落在 status/ 目录里，所以绝不能出现账号、姓名或密码。
    """
    payload = {"account_slug": privacy.slug(account["username"]), "paper": paper}
    for key in configmod.DEFAULTS:
        # 密码、Cookie 和账号清单都不能进任务文件
        if key in ("accounts", "paper", "password", "cookie"):
            continue
        payload[key] = cfg.get(key)
    payload["base_url"] = cfg.get("base_url")
    # 这几个路径不在 DEFAULTS 里，但 worker 必须和主进程用同一套目录：
    # 默认是"本次运行的临时目录"，每个进程各自 mkdtemp 出来的是不同目录，
    # 不显式传过去，主进程就会在自己的 status 目录里等一个永远不出现的结果文件。
    for key in ("log_dir", "raw_dir", "status_dir", "cache_path"):
        payload[key] = cfg.get(key)
    return payload


def write_job(cfg, account, paper):
    slug = privacy.slug(account["username"])
    path = os.path.join(cfg.get("status_dir") or "status", "job-%s.json" % slug)
    ui.ensure_dir(os.path.dirname(os.path.abspath(path)))
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(job_payload(cfg, account, paper), ensure_ascii=False, indent=1))
    return path


def worker_command(job_path):
    """冻结成 exe 与源码运行两种形态都能拉起 worker。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", "--job", job_path]
    return [sys.executable, "-m", "autoexam.worker", "--job", job_path]


def spawn_one(cfg, account, paper, logger, new_console=True):
    job_path = write_job(cfg, account, paper)
    cmd = worker_command(job_path)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 账号与密码只走环境变量，不写进任务文件（任务文件会留在 status/ 目录里）
    env["AUTOEXAM_ACCOUNT"] = account["username"]
    env["AUTOEXAM_PASSWORD"] = account["password"]
    kwargs = {
        "env": env,
        "cwd": cfg.get("root") or os.getcwd(),
    }
    if new_console and os.name == "nt":
        kwargs["creationflags"] = CREATE_NEW_CONSOLE
    try:
        process = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        logger.error("拉起 %s 失败：%s" % (account["username"], exc))
        return None
    logger.info("已启动 %s（pid=%s，%s）" % (account["username"], process.pid, job_path))
    return process


def monitor(cfg, processes, logger):
    """盯着子进程，定期打一行汇总，最后出成绩表。"""
    status_dir = cfg.get("status_dir") or "status"
    slugs = {}
    for account, process in processes:
        logger.add_secret(account["username"])
        logger.add_secret(account["password"])
        slugs[privacy.slug(account["username"])] = (account["username"], process)

    last_report = 0.0
    while True:
        alive = [item for item in processes if item[1].poll() is None]
        now = time.time()
        if now - last_report >= 8.0:
            last_report = now
            line = _summary_line(status_dir, slugs)
            if line:
                logger.plain("  进度  %s" % line)
        if not alive:
            break
        time.sleep(1.0)

    logger.plain("")
    logger.plain("=" * 72)
    logger.plain("  全部账号已结束，结果如下：")
    logger.plain("-" * 72)
    for account, process in processes:
        slug = privacy.slug(account["username"])
        result_path = os.path.join(status_dir, "result-%s.json" % slug)
        result = ui.read_status(result_path)
        if not result:
            logger.plain("  %-16s 没有产出结果（退出码 %s）" % (account["username"], process.returncode))
            continue
        summary = result.get("summary") or {}
        score = _num(summary.get("score"))
        max_score = _num(summary.get("max_score"))
        counts = result.get("counts") or {}
        logger.plain("  %-16s %s/%s  正确率 %s%%  %s" % (
            account["username"], _fmt(score), _fmt(max_score),
            _fmt(summary.get("rate")),
            "未解决 %d 题" % len(result.get("unresolved") or []) if result.get("unresolved") else "全部命中",
        ))
        logger.plain("      路线：错题本 %s · 缓存 %s · 接口直给 %s · 探针直中 %s" % (
            counts.get("wrongbook", 0), counts.get("cache", 0),
            counts.get("answer_api", 0) + counts.get("questions", 0),
            counts.get("unknown", 0),
        ))
        logger.plain("      日志：%s" % (result.get("log") or "-"))
    logger.plain("=" * 72)
    logger.plain("")


def _summary_line(status_dir, slugs):
    parts = []
    for slug, (username, process) in slugs.items():
        state = ui.read_status(os.path.join(status_dir, "%s.json" % slug)) or {}
        phase = state.get("phase") or "等待"
        done = state.get("done") or 0
        total = state.get("total") or 0
        if process.poll() is not None and phase != "finished":
            phase = "已退出"
        if total:
            parts.append("%s %s %s/%s" % (username, phase, done, total))
        else:
            parts.append("%s %s" % (username, phase))
    return " | ".join(parts)


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value):
    number = _num(value)
    if abs(number - round(number)) < 0.01:
        return str(int(round(number)))
    return "%.1f" % number


def run_single(cfg, logger, username, password, paper):
    """在当前进程里跑一个账号（调试或单开时用）。失败返回 None。"""
    try:
        return workermod.run(cfg, username, password, paper)
    except netio.NetworkError as exc:
        logger.error("连不上服务器：%s" % exc)
        logger.warn("检查网络是否能访问 %s（内网/校园网）" % (cfg.get("base_url") or "目标地址"))
        return None
    except netio.Unauthorized as exc:
        logger.error("登录态失效：%s" % exc)
        return None
    except Exception as exc:
        logger.error("执行失败：%s" % (str(exc) or type(exc).__name__))
        return None


def run_multi(cfg, logger, accounts, paper):
    """每个账号一个独立控制台窗口。"""
    if not accounts:
        logger.error("没有账号。")
        return 1
    ui.ensure_dir(cfg.get("status_dir") or "status")
    new_console = bool(cfg.get("new_console", True))
    processes = []
    for index, account in enumerate(accounts):
        process = spawn_one(cfg, account, paper, logger, new_console=new_console)
        if process is not None:
            processes.append((account, process))
        time.sleep(0.4)
    if not processes:
        logger.error("一个都没启动成功。")
        return 1
    logger.plain("")
    if new_console:
        logger.info("已拉起 %d 个实例，各自有独立窗口。主窗口这里只做汇总。" % len(processes))
        logger.info("想中止某个实例：直接关掉它的窗口即可。")
    else:
        logger.info("已拉起 %d 个实例（共用当前控制台输出）。" % len(processes))
    monitor(cfg, processes, logger)
    return 0
