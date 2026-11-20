# -*- coding: utf-8 -*-
"""配置与命令行参数。

优先级：内置默认值 < config.json < 命令行参数。
布尔项统一用 --xxx / --no-xxx 成对形式，默认 None 表示"命令行没表态"，
这样 config.json 里的设置不会被默认值悄悄覆盖。
"""

import argparse
import io
import json
import os
import sys
import urllib.parse


def _project_root():
    """源码运行时是项目根目录；打包成 exe 后是 exe 所在目录。

    冻结后一定要用 exe 旁边的目录，否则配置、日志、答案缓存会写进
    PyInstaller 的临时解压目录（onefile 退出即消失）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = _project_root()
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config.json")
URL_FILE_CANDIDATES = ["刷题网址.txt", "url.txt", "地址.txt"]
LOG_DIR = os.path.join(ROOT, "logs")
RAW_DIR = os.path.join(LOG_DIR, "raw")
STATUS_DIR = os.path.join(ROOT, "status")
CACHE_PATH = os.path.join(ROOT, "answer_cache.json")

DEFAULTS = {
    # 服务器
    "base_url": "",
    "password": "123456",
    "cookie": "",
    "http_timeout": 15,
    "retries": 3,
    "insecure_tls": False,
    "proxy": "",
    # 登录页（自动探测失败时可以硬指）
    "login_url": "",
    "login_user_field": "",
    "login_pass_field": "",
    # 做题
    "mode": "exam",
    "fresh": True,
    "points_opt_in": True,
    "strategy": "per-question",
    "delay_min": 7.0,
    "delay_max": 13.0,
    "assume_limit": 900,
    "submit": True,
    "final_check": True,
    # 错题本 / 匹配
    "poll_interval": 0.8,
    "poll_tries": 3,
    "wrong_session": "same",
    "probe_fill": "X",
    "trust_answer_api": "auto",
    "trust_questions_answer": "auto",
    "allow_formal": False,
    # 缓存
    "use_cache": True,
    # 杂项
    # 默认不把任何响应落盘：响应里可能带姓名、成绩、错题与 Cookie。
    # 需要排查接口时用 --save-raw 显式打开（落盘内容同样会做账号脱敏）。
    "save_raw": False,
    "input_wait": True,
    "new_console": True,
    "paper": None,
    "accounts": [],
}


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "是")


def load_config(path=None, cli_overrides=None):
    """读 config.json，再叠加命令行上真正给了值的项。"""
    cfg = dict(DEFAULTS)
    path = path or DEFAULT_CONFIG_PATH
    if path and os.path.isfile(path):
        try:
            with io.open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if key in DEFAULTS:
                        cfg[key] = value
                    else:
                        cfg[key] = value
        except Exception as exc:
            cfg["_config_error"] = "读取 %s 失败：%s" % (path, exc)
    for key, value in (cli_overrides or {}).items():
        if value is not None:
            cfg[key] = value
    cfg["config_path"] = path
    cfg["root"] = ROOT
    # 这些是路径类设置：调用方（含测试）显式给了就尊重，没给才用默认值。
    cfg.setdefault("log_dir", LOG_DIR)
    cfg.setdefault("raw_dir", RAW_DIR)
    cfg.setdefault("status_dir", STATUS_DIR)
    cfg.setdefault("cache_path", CACHE_PATH)
    cfg["base_url"] = resolve_base_url(cfg.get("base_url"))
    if cfg.get("delay_max") is not None and cfg.get("delay_min") is not None:
        if float(cfg["delay_max"]) < float(cfg["delay_min"]):
            cfg["delay_max"] = cfg["delay_min"]
    return cfg


def resolve_base_url(given):
    """给定值优先；否则从用户说的"新建文本文档.txt"里读网址。"""
    if given and str(given).strip():
        return _strip_base(str(given).strip())
    url = read_url_from_txt()
    if url:
        return _strip_base(url)
    return ""


def _strip_base(url):
    """去掉 hash / 查询 / 插件路径，只留 scheme://host:port。"""
    url = (url or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    if not parts.netloc:
        return ""
    return "%s://%s" % (parts.scheme, parts.netloc.rstrip("/"))


def read_url_from_txt(root=None):
    """在工作区里找一个写着刷题网址的 txt，返回里面第一个 http(s) 链接。"""
    root = root or ROOT
    names = []
    for name in URL_FILE_CANDIDATES:
        names.append(os.path.join(root, name))
    try:
        for entry in sorted(os.listdir(root)):
            if entry.lower().endswith(".txt"):
                names.append(os.path.join(root, entry))
    except OSError:
        pass
    seen = set()
    for path in names:
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                with io.open(path, "r", encoding=encoding) as fh:
                    for line in fh:
                        line = line.strip()
                        if "http://" in line or "https://" in line:
                            return line
                break
            except Exception:
                continue
    return ""


def parse_practice_url(url):
    """从 '#/practice/2?mode=exam&points=1' 里取出 paper_id/mode/points。"""
    info = {}
    url = url or ""
    marker = "/practice/"
    pos = url.find(marker)
    if pos < 0:
        return info
    rest = url[pos + len(marker):]
    for sep in ("?", "#"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    if digits:
        info["paper_id"] = int(digits)
    if "?" in url:
        query = url.split("?", 1)[1]
        parsed = urllib.parse.parse_qs(query.split("#")[0])
        if "mode" in parsed:
            info["mode"] = parsed["mode"][0]
        if "points" in parsed:
            info["points"] = parsed["points"][0]
    return info


def detect_url_hint(root=None):
    """把 txt 里的网址解析成 {base_url, paper_id, mode, points}。"""
    url = read_url_from_txt(root)
    if not url:
        return {}
    hint = parse_practice_url(url)
    hint["base_url"] = _strip_base(url)
    hint["source_url"] = url
    return hint


def build_parser():
    """共用参数表：主进程、worker、mock 都用它（各自只取自己关心的）。"""
    parser = argparse.ArgumentParser(
        prog="autoexam",
        description="考试中心自动刷题助手（纯标准库，Win7/10/11 通用）",
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    parser.add_argument("--base", dest="base_url", default=None,
                        help="服务器根地址，如 http://10.0.0.10:5000；缺省自动读网址 txt")
    parser.add_argument("--account", dest="account", default=None, help="单个账号（worker 模式必填）")
    parser.add_argument("--accounts", dest="accounts_raw", default=None,
                        help="多个账号，逗号或空格分隔；也支持 账号:密码")
    parser.add_argument("--password", default=None, help="统一密码（默认 123456）")
    parser.add_argument("--cookie", default=None, help="直接给 Cookie 字符串（登录页搞不定时的兜底）")
    parser.add_argument("--paper", dest="paper", type=int, default=None, help="试卷 id（不做交互选择）")
    parser.add_argument("--all-points", dest="all_points", action="store_true", default=None,
                        help="全部积分：把所有还能计分的卷（含同一卷剩余次数）依次刷完")
    parser.add_argument("--mode", default=None, choices=["exam", "practice"], help="exam=模考（默认）")
    parser.add_argument("--strategy", default=None, choices=["per-question", "batch"],
                        help="per-question=一题一轮（默认）；batch=先全探针再一次回填")
    parser.add_argument("--delay-min", dest="delay_min", type=float, default=None, help="每题最短耗时（秒）")
    parser.add_argument("--delay-max", dest="delay_max", type=float, default=None, help="每题最长耗时（秒）")
    parser.add_argument("--assume-limit", dest="assume_limit", type=float, default=None,
                        help="假定考试时限（秒），用于判断要不要切换 batch 策略")
    parser.add_argument("--poll-interval", dest="poll_interval", type=float, default=None,
                        help="查错题本的轮询间隔（秒）")
    parser.add_argument("--poll-tries", dest="poll_tries", type=int, default=None,
                        help="查错题本最多轮询几次")
    parser.add_argument("--probe-fill", dest="probe_fill", default=None, help="填空题探针答案（默认 X）")
    parser.add_argument("--timeout", dest="http_timeout", type=float, default=None, help="单请求超时（秒）")
    parser.add_argument("--retries", type=int, default=None, help="网络失败重试次数")
    parser.add_argument("--wrong-session", dest="wrong_session", default=None,
                        choices=["same", "second"],
                        help="查错题本的会话：same=同会话；second=另开同账号第二会话")
    parser.add_argument("--trust-answer-api", dest="trust_answer_api", default=None,
                        choices=["auto", "on", "off"],
                        help="是否相信 /answer 返回的 correct_answer")
    parser.add_argument("--trust-questions-answer", dest="trust_questions_answer", default=None,
                        choices=["auto", "on", "off"],
                        help="是否相信 /api/attempts 题目里的 answer 字段")
    parser.add_argument("--paper-json", dest="paper_json", default=None,
                        help="内部用：主进程把选中的试卷 JSON 传给 worker")
    parser.add_argument("--job", dest="job", default=None,
                        help="内部用：worker 读取主进程写好的任务文件")
    parser.add_argument("--worker", dest="worker", action="store_true", default=None,
                        help="内部用：以工作进程身份启动（冻结成 exe 时用）")
    parser.add_argument("--login-url", dest="login_url", default=None,
                        help="登录页地址（默认 /login?next=/plugin/examcenter/）")
    parser.add_argument("--login-user-field", dest="login_user_field", default=None,
                        help="登录表单里账号字段的 name（自动探测失败时硬指）")
    parser.add_argument("--login-pass-field", dest="login_pass_field", default=None,
                        help="登录表单里密码字段的 name（自动探测失败时硬指）")

    bools = [
        ("--fresh", "fresh", "从头开始作答（勾选'重新开始'）", True),
        ("--points", "points_opt_in", "把本次成绩计入个人积分", True),
        ("--submit", "submit", "做完自动交卷", True),
        ("--final-check", "final_check", "交卷前再用错题本复核一轮", True),
        ("--cache", "use_cache", "使用本地答案缓存", True),
        ("--allow-formal", "allow_formal", "允许刷正式考试卷（formal_exam）", False),
        ("--save-raw", "save_raw", "把每个请求的原始响应落盘到 logs/raw（默认关，排查接口时才开）", False),
        ("--input-wait", "input_wait", "结束后等待回车（独立窗口模式用）", True),
        ("--new-console", "new_console", "每个账号开独立控制台窗口", True),
        ("--insecure-tls", "insecure_tls", "忽略 HTTPS 证书校验", False),
    ]
    for flag, dest, help_text, default in bools:
        parser.add_argument(flag, dest=dest, action="store_true", default=None, help=help_text)
        parser.add_argument("--no-" + flag[2:], dest=dest, action="store_false",
                            default=None, help="关闭：" + help_text)
    return parser


def cli_config(argv=None):
    """解析命令行并合并配置。返回 (cfg, args)。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    raw = vars(args)
    if raw.get("accounts_raw"):
        raw["accounts"] = split_accounts(raw["accounts_raw"])
    cfg = load_config(raw.get("config"), raw)
    cfg["argv"] = list(argv) if argv is not None else []
    return cfg, args


def split_accounts(text):
    """把 'a,b c' 拆成 [{'username':..,'password':..}]。"""
    items = []
    for chunk in str(text or "").replace(",", " ").replace("，", " ").replace(";", " ").split():
        chunk = chunk.strip()
        if not chunk:
            continue
        username, sep, password = chunk.partition(":")
        username = username.strip()
        if not username:
            continue
        items.append({"username": username, "password": password.strip() if sep else ""})
    return items


def load_accounts_file(path):
    """读账号清单：每行 '账号' 或 '账号,密码'，支持 # 注释。"""
    result = []
    if not path or not os.path.isfile(path):
        return result
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with io.open(path, "r", encoding=encoding) as fh:
                lines = fh.read().splitlines()
            break
        except Exception:
            lines = []
            continue
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for piece in line.replace(",", " ").replace("，", " ").split():
            username, sep, password = piece.partition(":")
            username = username.strip()
            if username:
                result.append({"username": username, "password": password.strip() if sep else ""})
    return result
