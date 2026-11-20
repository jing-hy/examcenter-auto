# -*- coding: utf-8 -*-
"""终端交互与日志。

要点：
1. 中文输出在 Win7 的 GBK 控制台里可能编码失败，统一走 safe_print 兜底；
2. 日志同时写控制台与文件，多线程安全；
3. 状态文件用于多开时主进程汇总子进程进度（原子替换写）。
"""

import io
import json
import os
import sys
import threading
import time

from . import privacy

_LOCK = threading.RLock()


def _init_console():
    """尽量让标准输出按 UTF-8 工作；不支持就安静放弃。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_init_console()


def safe_print(text="", end="\n", stream=None):
    """永不因编码问题炸掉的 print。"""
    stream = stream or sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + end)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        try:
            stream.write(text.encode(enc, "replace").decode(enc, "replace") + end)
        except Exception:
            return
    except Exception:
        return
    try:
        stream.flush()
    except Exception:
        pass


def ensure_dir(path):
    """确保目录存在，返回该目录。"""
    if path and not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            if not os.path.isdir(path):
                raise
    return path


class Logger(object):
    """带时间戳与等级前缀、同时落文件的日志器。

    落盘的内容会经过 privacy.redact_text 脱敏：账号、姓名、密码、Cookie
    都替换成 ***，控制台输出保持原样（方便人看）。
    """

    INFO = "信息"
    OK = "成功"
    WARN = "警告"
    ERR = "错误"
    DBG = "调试"

    def __init__(self, path=None, echo=True, prefix="", file_prefix=None, secrets=None):
        self.path = path
        self.echo = echo
        self.prefix = prefix
        self.file_prefix = prefix if file_prefix is None else file_prefix
        self.secrets = privacy.clean_secrets(secrets)
        if path:
            ensure_dir(os.path.dirname(os.path.abspath(path)))

    def add_secret(self, *values):
        """发现新的敏感串（例如登录后才知道的姓名）就登记，之后落盘自动脱敏。"""
        self.secrets = privacy.clean_secrets(list(self.secrets) + list(values))

    def _file_text(self, text):
        return privacy.redact_text(text, self.secrets)

    def line(self, tag, msg):
        stamp = time.strftime("%H:%M:%S")
        head = "[%s]" % stamp
        if self.prefix:
            head += "[%s]" % self.prefix
        text = "%s[%s] %s" % (head, tag, msg)
        file_head = "[%s]" % stamp
        if self.file_prefix:
            file_head += "[%s]" % self.file_prefix
        file_text = "%s[%s] %s" % (file_head, tag, self._file_text(msg))
        with _LOCK:
            if self.echo:
                safe_print(text)
            if self.path:
                try:
                    with io.open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(file_text + "\n")
                except Exception:
                    pass
        return text

    def info(self, msg):
        return self.line(self.INFO, msg)

    def ok(self, msg):
        return self.line(self.OK, msg)

    def warn(self, msg):
        return self.line(self.WARN, msg)

    def error(self, msg):
        return self.line(self.ERR, msg)

    def debug(self, msg):
        return self.line(self.DBG, msg)

    def plain(self, msg):
        """不带等级前缀的整行输出（用于菜单、表格）。"""
        with _LOCK:
            if self.echo:
                safe_print(msg)
            if self.path:
                try:
                    with io.open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(self._file_text(msg) + "\n")
                except Exception:
                    pass


def ask(prompt, default=None, allow_empty=True):
    """读一行输入；allow_empty=False 时为空则重问。"""
    while True:
        safe_print(prompt, end="")
        try:
            raw = input()
        except (EOFError, KeyboardInterrupt):
            safe_print("")
            raise
        value = (raw or "").strip()
        if not value:
            if default is not None:
                return default
            if allow_empty:
                return ""
            safe_print("  输入不能为空，请重新输入。")
            continue
        return value


def ask_int(prompt, default=None, low=None, high=None):
    """读一个整数，带范围校验。"""
    while True:
        raw = ask(prompt, default=("" if default is None else str(default)))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            safe_print("  请输入数字。")
            continue
        if low is not None and value < low:
            safe_print("  不能小于 %d。" % low)
            continue
        if high is not None and value > high:
            safe_print("  不能大于 %d。" % high)
            continue
        return value


def ask_yes_no(prompt, default=True):
    """是/否确认，默认值影响空输入。"""
    tip = "Y/n" if default else "y/N"
    raw = ask("%s [%s] " % (prompt, tip), default="")
    if not raw:
        return bool(default)
    return raw.strip().lower() in ("y", "yes", "1", "是", "好", "对")


def write_status(path, data):
    """原子写状态 JSON（多开时主进程要读）。"""
    if not path:
        return
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    tmp = path + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=1))
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        pass


def read_status(path):
    """读状态 JSON，读不到返回 None。"""
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def fmt_seconds(seconds):
    """秒 -> mm:ss / h:mm:ss。"""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "00:00"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def shrink(text, width=36):
    """把长文本压成一行便于日志查看。"""
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def banner(logger, title, lines=None):
    """打印一个朴素的标题块（不用制表符，免得老控制台乱码）。"""
    logger.plain("")
    logger.plain("=" * 62)
    logger.plain("  " + title)
    if lines:
        for item in lines:
            logger.plain("  " + item)
    logger.plain("=" * 62)
    logger.plain("")
