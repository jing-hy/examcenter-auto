# -*- coding: utf-8 -*-
"""离线依赖验证：证明这套程序运行期不需要任何外部网络资源。

三块证据：
  1. 静态 import 检查——只用标准库；
  2. 静态 URL 扫描——代码里没有藏起来的远程地址；
  3. 动态审计——用 sys.addaudithook 抓整个流程的所有网络事件，
     断言除了本机回环地址，没有任何出站连接。

    python tools/check_offline.py
"""

import io
import os
import re
import subprocess
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ACCOUNT = "demo001"
PASSWORD = "123456"

# 允许出现的"外部"域名（都不是运行期必需项，逐条说明理由）
ALLOWED_HOSTS = {
    "127.0.0.1": "本机回环（离线靶场）",
    "localhost": "本机回环",
    "10.42.125.67": "内置候选地址：启动时做一次 TCP 探活，连不上就跳过",
    "www.python.org": "只出现在 tools/make_portable.ps1：可选的一次性准备步骤",
    "python.org": "同上",
    "github.com": "只出现在 README 的下载链接，运行期不访问",
}

URL_RE = re.compile(r"https?://[^\s\"'()<>,\uff0c\u3002\uff09]+")
SCAN_EXTS = (".py", ".ps1", ".bat", ".json", ".txt", ".md")
SKIP_DIRS = {".dsh-meow", "runtime", "logs", "status", "dist", "build", "__pycache__"}

LOCAL_MODULES = {"autoexam", "mock", "seed"}


# --------------------------------------------------------------------------- 1
def check_imports():
    """所有 import 必须落在标准库或本项目内。"""
    problems = []
    seen = set()
    for path in iter_sources():
        try:
            with io.open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except Exception:
            continue
        for line in source.splitlines():
            line = line.strip()
            if line.startswith("import "):
                names = [item.split(" as ")[0].strip().split(".")[0]
                         for item in line[7:].split(",")]
            elif line.startswith("from ") and " import " in line:
                names = [line[5:].split(" import ")[0].strip().split(".")[0]]
            else:
                continue
            for name in names:
                if not name or name in seen:
                    continue
                seen.add(name)
                if name in LOCAL_MODULES:
                    continue
                if not is_stdlib(name):
                    problems.append("%s: import %s" % (
                        os.path.relpath(path, ROOT), name))
    return sorted(set(seen)), problems


def iter_sources():
    for base in ("autoexam", "mock", "tools", "tests"):
        folder = os.path.join(ROOT, base)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                yield os.path.join(folder, name)
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py"):
            yield os.path.join(ROOT, name)


def is_stdlib(name):
    if name in sys.builtin_module_names:
        return True
    # Python 3.10+ 直接有权威列表
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return name in names
    try:
        import importlib.util
        spec = importlib.util.find_spec(name)
    except Exception:
        return False
    if spec is None:
        return False
    origin = spec.origin or ""
    if origin in ("built-in", "frozen", "namespace"):
        return True
    try:
        import sysconfig
        paths = sysconfig.get_paths()
    except Exception:
        return True
    roots = [paths.get("stdlib"), paths.get("platstdlib")]
    # 扩展模块（zlib/unicodedata 之类）装在 DLLs 目录下
    if paths.get("stdlib"):
        roots.append(os.path.join(os.path.dirname(paths["stdlib"]), "DLLs"))
    real = os.path.realpath(origin)
    for root in roots:
        if root and real.startswith(os.path.realpath(root)):
            return True
    return False


# --------------------------------------------------------------------------- 2
def scan_urls():
    """列出源码里出现的所有 http(s) 字面量，按 host 归类。

    形如 "http://%s:%d" 的格式化模板会被跳过（运行期才拼出真实地址）。
    """
    found = {}
    templates = 0
    for path in iter_url_files():
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except Exception:
            continue
        for match in URL_RE.finditer(source):
            url = match.group(0).rstrip(").,;")
            host = urllib.parse.urlsplit(url).hostname or ""
            if not host:
                continue
            if "%" in host or "{" in host or "$" in host:
                templates += 1
                continue
            rel = os.path.relpath(path, ROOT)
            found.setdefault(host, set()).add(rel)
    return found, templates


def iter_url_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            if "_files" in dirpath:
                continue
            if not name.lower().endswith(SCAN_EXTS):
                continue
            if name.startswith("考试中心"):  # 只读参考页面
                continue
            yield os.path.join(dirpath, name)


# --------------------------------------------------------------------------- 3
def audit_flow():
    """装审计钩子跑一遍完整流程，记录所有网络事件。"""
    events = []
    net_events = (
        "socket.connect", "socket.getaddrinfo", "socket.bind",
        "urllib.Request", "http.client.connect", "ssl.SSLContext.wrap_socket",
    )

    def hook(event, args):
        if event not in net_events:
            return
        try:
            events.append((event, args))
        except Exception:
            pass

    sys.addaudithook(hook)

    from autoexam import api as apimod
    from autoexam import config as configmod
    from autoexam import login as loginmod
    from autoexam import matcher
    from autoexam import netio
    from autoexam import solver as solvermod
    from autoexam import ui
    from mock import server as mockserver

    import tempfile
    import shutil

    tmp = tempfile.mkdtemp(prefix="offline-")
    srv = None
    result = None
    try:
        srv, thread, db = mockserver.start_server(port=0, question_budget=8, paper_count=1)
        port = srv.server_address[1]
        cfg = configmod.load_config(None, {
            "base_url": "http://127.0.0.1:%d" % port,
            "delay_min": 0.01,
            "delay_max": 0.02,
            "poll_interval": 0.02,
            "poll_tries": 3,
            "save_raw": False,
            "log_dir": os.path.join(tmp, "logs"),
            "raw_dir": os.path.join(tmp, "raw"),
            "status_dir": os.path.join(tmp, "status"),
            "cache_path": os.path.join(tmp, "cache.json"),
        })
        logger = ui.Logger(None, echo=False)
        client = netio.HttpClient(cfg["base_url"], timeout=10, retries=2,
                                  save_raw=False, logger=logger)
        api_client = apimod.ExamCenter(client, logger)
        loginmod.login(cfg, client, logger, ACCOUNT, PASSWORD)
        paper = api_client.papers()[0]
        cache = matcher.AnswerCache(cfg["cache_path"], True, logger)
        engine = solvermod.Solver(cfg, api_client, logger, ACCOUNT, paper, cache)
        result = engine.run()
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    return events, result


def hosts_from_events(events):
    """把审计事件里的主机名抽出来。"""
    hosts = []
    for event, args in events:
        host = None
        try:
            if event == "urllib.Request":
                host = urllib.parse.urlsplit(str(args[0])).hostname
            elif event in ("socket.connect", "socket.bind"):
                address = args[1] if len(args) > 1 else None
                if isinstance(address, tuple) and address:
                    host = address[0]
            elif event == "socket.getaddrinfo":
                host = args[0] if args else None
            elif event == "http.client.connect":
                host = args[1] if len(args) > 1 else None
        except Exception:
            host = None
        if host:
            hosts.append((event, str(host)))
    return hosts


def is_local(host):
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "::1", "::ffff:127.0.0.1"):
        return True
    if host.startswith("127."):
        return True
    return False


# --------------------------------------------------------------------------- 4
def check_isolated_startup():
    """用 -I -S（隔离模式、不加载 site 包）启动，证明不依赖已安装的第三方包。"""
    cmd = [sys.executable, "-I", "-S", os.path.join(ROOT, "run_autoexam.py"), "--help"]
    process = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    out, err = process.communicate()
    text = out.decode("utf-8", "replace")
    return process.returncode == 0 and "考试中心自动刷题助手" in text, text[:200]


# --------------------------------------------------------------------------- main
def main():
    failures = []

    print("=" * 70)
    print("  1/4  静态 import 检查（是否只依赖标准库）")
    print("=" * 70)
    modules, problems = check_imports()
    print("  用到的模块：%s" % ", ".join(sorted(modules)))
    if problems:
        failures.extend(problems)
        for item in problems:
            print("  [FAIL] 非标准库依赖：%s" % item)
    else:
        print("  [ OK ] 全部落在 Python 标准库内，没有第三方包。")

    print("")
    print("=" * 70)
    print("  2/4  静态 URL 扫描（代码里有没有藏远程地址）")
    print("=" * 70)
    hosts, templates = scan_urls()
    if not hosts:
        print("  [ OK ] 源码里没有任何 http(s) 字面量。")
    if templates:
        print("  [ OK ] 跳过 %d 处格式化模板（如 \"http://%%s:%%d\"，运行期才拼地址）" % templates)
    for host in sorted(hosts):
        files = sorted(hosts[host])
        note = ALLOWED_HOSTS.get(host)
        if note:
            print("  [ OK ] %-16s %s" % (host, note))
            print("         出现于：%s" % ", ".join(files))
        else:
            failures.append("未登记的外部域名：%s" % host)
            print("  [FAIL] %-16s 未登记的外部域名！出现于：%s" % (host, ", ".join(files)))

    print("")
    print("=" * 70)
    print("  3/4  动态审计（跑一遍完整流程，抓所有网络事件）")
    print("=" * 70)
    events, result = audit_flow()
    entries = hosts_from_events(events)
    external = [(event, host) for event, host in entries if not is_local(host)]
    print("  抓到 %d 条网络事件。" % len(entries))
    counts = {}
    for event, host in entries:
        counts[(event, host)] = counts.get((event, host), 0) + 1
    for (event, host), count in sorted(counts.items()):
        flag = "本机" if is_local(host) else "外部!"
        print("    %-22s %-16s x%d  [%s]" % (event, host, count, flag))
    if external:
        failures.append("流程里出现了外部网络访问")
        for event, host in external:
            print("  [FAIL] 访问了外部地址：%s -> %s" % (event, host))
    else:
        print("  [ OK ] 全部指向本机回环，没有任何外部出站连接。")
    summary = (result or {}).get("summary") or {}
    print("  顺带确认流程真的跑完了：得分 %s/%s，答对 %s/%s 题" % (
        summary.get("score"), summary.get("max_score"),
        summary.get("correct"), summary.get("total")))
    if not summary:
        failures.append("离线流程没有跑完")

    print("")
    print("=" * 70)
    print("  4/4  隔离模式启动（python -I -S，不加载 site 第三方包）")
    print("=" * 70)
    ok, head = check_isolated_startup()
    if ok:
        print("  [ OK ] 在 python -I -S 下仍能正常启动并打印帮助。")
    else:
        failures.append("隔离模式下启动失败")
        print("  [FAIL] 启动输出：%s" % head)

    print("")
    print("=" * 70)
    if failures:
        print("  结论：不通过（%d 个问题）" % len(failures))
        for item in failures:
            print("    - %s" % item)
        print("=" * 70)
        return 1
    print("  结论：通过 —— 运行期不需要任何外部网络资源。")
    print("  唯一的出站目标是你自己的做题服务器；离线靶场走 127.0.0.1。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
