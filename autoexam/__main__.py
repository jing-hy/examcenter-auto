# -*- coding: utf-8 -*-
"""命令行入口与主菜单。

    python run_autoexam.py              交互菜单
    python -m autoexam                  同上
    python -m autoexam --account demo001 --paper 2
"""

import os
import sys
import time

from . import config as configmod
from . import netio
from . import supervisor
from . import ui

HELP_TEXT = u"""
用法示例
--------
  交互菜单             python run_autoexam.py
  单账号直接跑         python run_autoexam.py --account demo001 --paper 2
  多账号多开           python run_autoexam.py --accounts "demo001,demo002"
  指定服务器           python run_autoexam.py --base http://10.0.0.10:5000
  登录页搞不定时       python run_autoexam.py --account 学号 --cookie "SESSION=xxxx"
  先离线试跑           python mock/server.py --port 8420
                       python run_autoexam.py --base http://127.0.0.1:8420 --account demo001

常用参数
--------
  --delay-min / --delay-max   每题耗时区间（秒），默认 7 / 13
  --strategy                   per-question（默认）或 batch
  --no-fresh                  不清空旧进度（默认会勾"重新开始"）
  --no-points                 不勾"计入个人积分"
  --no-submit                 做完不交卷（只验证流程，别动成绩）
  --paper N                   直接指定试卷 id
  --allow-formal              允许刷正式考试卷（默认拒绝）
  --save-raw / --no-save-raw  是否把每次响应存到 logs/raw
  --help                      完整参数表
"""


def _logger(cfg):
    stamp = time.strftime("%Y%m%d")
    path = os.path.join(cfg.get("log_dir") or "logs", "main-%s.log" % stamp)
    return ui.Logger(path, prefix="主")


def _banner(cfg, logger):
    hint = configmod.detect_url_hint()
    lines = []
    base = cfg.get("base_url") or "（未找到，请用 --base 指定）"
    lines.append("服务器    %s" % base)
    if hint.get("paper_id"):
        lines.append("网址提示  试卷 id=%s  模式=%s  计入积分=%s" %
                     (hint.get("paper_id"), hint.get("mode") or "exam",
                      hint.get("points") or "?"))
    lines.append("每题延时  %s ~ %s 秒" % (_seconds(cfg.get("delay_min")),
                                          _seconds(cfg.get("delay_max"))))
    lines.append("策略      %s" % cfg.get("strategy"))
    lines.append("密码      %s" % ("******" if cfg.get("password") else "（未设置）"))
    lines.append("日志目录  %s" % (cfg.get("log_dir") or "logs"))
    ui.banner(logger, "考试中心 · 自动刷题助手", lines)


def _seconds(value):
    """把秒数显示成人能读的形式（0.05 秒别被 %.0f 吃掉）。"""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if abs(number - round(number)) < 0.01:
        return str(int(round(number)))
    return "%.2f" % number


def _ensure_dirs(cfg):
    keys = ["log_dir", "status_dir"]
    # 只有明确要存原始响应时才建 logs/raw，默认不落任何响应
    if cfg.get("save_raw"):
        keys.append("raw_dir")
    for key in keys:
        path = cfg.get(key)
        if path:
            try:
                ui.ensure_dir(path)
            except OSError as exc:
                ui.safe_print("建目录失败 %s：%s" % (path, exc))


def _menu(cfg, logger):
    while True:
        logger.plain("")
        logger.plain("  请选择：")
        logger.plain("    1) 单账号刷题（就在这个窗口里跑，日志直接看得见）")
        logger.plain("    2) 多账号多开（每个账号一个独立控制台窗口）")
        logger.plain("    3) 启动本地模拟服务器（离线验收用）")
        logger.plain("    4) 看看当前配置")
        logger.plain("    5) 帮助")
        logger.plain("    0) 退出")
        logger.plain("")
        logger.plain("  提示：选卷那一步输入 A = 全部积分，把还能计分的卷（含剩余次数）一次刷完。")
        logger.plain("")
        choice = ui.ask("  输入序号：", allow_empty=False)
        if choice in ("0", "q", "Q", "exit"):
            logger.plain("  再见。")
            return 0
        if choice == "1":
            _session(cfg, logger, multi=False)
        elif choice == "2":
            _session(cfg, logger, multi=True)
        elif choice == "3":
            _run_mock(cfg, logger)
        elif choice == "4":
            _show_config(cfg, logger)
        elif choice == "5":
            logger.plain(HELP_TEXT)
        else:
            logger.plain("  没有这个选项。")


def _session(cfg, logger, multi):
    if not cfg.get("base_url"):
        base = ui.ask("  请输入服务器地址（如 http://10.0.0.10:5000）：", allow_empty=False)
        cfg["base_url"] = netio.normalize_base(base)
    logger.plain("")
    logger.plain("  -- 登录 --")
    username = ui.ask("  账号：", allow_empty=False)
    password = ui.ask("  密码（直接回车用默认 %s）：" % (cfg.get("password") or "123456"),
                      default=cfg.get("password") or "123456")

    try:
        client, api, user = supervisor.probe_login(cfg, logger, username, password)
    except netio.NetworkError as exc:
        logger.error("连不上服务器：%s" % exc)
        logger.warn("检查一下是不是不在校内网、或者地址写错了。")
        return
    except Exception as exc:
        logger.error("登录失败：%s" % exc)
        return

    paper = supervisor.pick_paper(api, logger, cfg)
    if not paper:
        logger.warn("没有选中试卷，已返回菜单。")
        return

    logger.plain("")
    logger.plain("  -- 确认 --")
    if supervisor.is_plan(paper):
        for line in supervisor.describe_plan(paper):
            logger.plain("  " + line)
        logger.plain("  说明：每套卷会按剩余次数反复作答，每次都会勾选「计入积分」。")
    else:
        logger.plain("  试卷：%s（%s 题，满分 %s）" %
                     (paper.get("name"), paper.get("total"), paper.get("max_score")))
    logger.plain("  模式：%s" % ("模拟考试（计入成绩）" if cfg.get("mode") == "exam" else "练习"))
    logger.plain("  计时：每题 %.0f~%.0f 秒" % (float(cfg.get("delay_min") or 0),
                                          float(cfg.get("delay_max") or 0)))
    if not ui.ask_yes_no("  开始吗？", default=True):
        return

    if multi:
        accounts = supervisor.collect_accounts(cfg, username)
        # 登记账号/密码，主进程日志落盘时自动脱敏
        for item in accounts:
            logger.add_secret(item["username"], item.get("password"))
        _show_accounts(logger, accounts)
        if not ui.ask_yes_no("  确认多开？", default=True):
            return
        supervisor.run_multi(cfg, logger, accounts, paper)
    else:
        supervisor.run_single(cfg, logger, username, password, paper)
        try:
            ui.safe_print("")
            input("  按回车返回菜单……")
        except (EOFError, KeyboardInterrupt):
            pass


def _show_accounts(logger, accounts):
    """控制台显示完整账号，落盘时被脱敏（只留首字符 + 匿名 hash）。"""
    logger.plain("  将要跑 %d 个账号：" % len(accounts))
    for index, item in enumerate(accounts):
        logger.plain("    %2d. %s" % (index + 1, item["username"]))


def _run_mock(cfg, logger):
    logger.plain("")
    logger.plain("  启动本地契约服务器（离线靶场），按 Ctrl+C 回到菜单。")
    logger.plain("  默认账号 demo001 / 123456，端口 8420。")
    port = ui.ask_int("  端口（回车用 8420）：", default=8420, low=1024, high=65535)
    try:
        from mock import server as mockserver
    except ImportError as exc:
        logger.error("mock 包导入失败：%s（请在项目根目录运行）" % exc)
        return
    server, thread, db = mockserver.start_server(port=port, question_budget=12, ui_mode=True)
    logger.ok("已在 http://127.0.0.1:%d/plugin/examcenter/ 启动" % port)
    logger.plain("  另开一个窗口执行：")
    logger.plain("    python run_autoexam.py --base http://127.0.0.1:%d --account demo001" % port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    logger.info("mock 已停止。")


def _show_config(cfg, logger):
    logger.plain("")
    logger.plain("-" * 60)
    for key in sorted(configmod.DEFAULTS.keys()):
        value = cfg.get(key)
        if key in ("password", "cookie") and value:
            value = "******"
        logger.plain("  %-24s %s" % (key, value))
    logger.plain("-" * 60)
    logger.plain("  配置文件：%s" % (cfg.get("config_path") or "config.json"))


def _direct(cfg, logger):
    """命令行给了账号时，跳过菜单直接跑。"""
    if not cfg.get("base_url"):
        logger.error("缺少服务器地址。用 --base 指定，或把网址放进工作区的 txt。")
        return 2
    accounts = cfg.get("accounts") or []
    if not accounts and cfg.get("account"):
        accounts = [{"username": cfg["account"],
                     "password": cfg.get("password") or "123456"}]
    if not accounts:
        return _menu(cfg, logger)

    first = accounts[0]
    for item in accounts:
        logger.add_secret(item["username"], item.get("password"))
    if len(accounts) == 1 and cfg.get("paper") is not None and not cfg.get("all_points"):
        # 单账号 + 已经指定试卷：直接交给 worker，省掉一次多余的登录
        result = supervisor.run_single(cfg, logger, first["username"], first["password"], None)
        return 0 if result else 1
    try:
        client, api, user = supervisor.probe_login(cfg, logger, first["username"],
                                                   first["password"])
    except Exception as exc:
        logger.error("登录失败：%s" % exc)
        return 1
    paper = supervisor.pick_paper(api, logger, cfg)
    if not paper:
        return 1
    if len(accounts) > 1:
        return supervisor.run_multi(cfg, logger, accounts, paper)
    result = supervisor.run_single(cfg, logger, first["username"], first["password"], paper)
    return 0 if result else 1


def main(argv=None):
    cfg, args = configmod.cli_config(argv)
    _ensure_dirs(cfg)
    logger = _logger(cfg)
    _banner(cfg, logger)
    if cfg.get("_config_error"):
        logger.warn(cfg["_config_error"])

    if getattr(args, "worker", False):
        from . import worker
        return worker.main(argv)

    interactive = not (cfg.get("accounts") or cfg.get("account"))
    if interactive:
        return _menu(cfg, logger)
    return _direct(cfg, logger)


if __name__ == "__main__":
    sys.exit(main())
