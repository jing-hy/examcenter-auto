# -*- coding: utf-8 -*-
"""3.7 语法/API 兼容性静态检查。

本项目要在 Win7 + Python 3.8.10 上跑，而你选的是"不确定目标机有没有 Python"，
所以代码统一按 3.7 语法写（3.7 能跑的代码在 3.8~3.12 都能跑）。

    python tools/check_compat.py
"""

import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = (3, 7)
SCAN_DIRS = ["autoexam", "mock", "tools", "tests"]

# 运行时 API 红线（语法层面查不出来，只能正则兜）
FORBIDDEN = [
    (r"\.removeprefix\s*\(", "str.removeprefix() 需要 3.9+"),
    (r"\.removesuffix\s*\(", "str.removesuffix() 需要 3.9+"),
    (r"functools\.cached_property", "functools.cached_property 需要 3.8+"),
    (r"functools\.cache\b", "functools.cache 需要 3.9+"),
    (r"\bmath\.lcm\b", "math.lcm 需要 3.9+"),
    (r"\bmath\.nextafter\b", "math.nextafter 需要 3.9+"),
    (r"^\s*import\s+zoneinfo", "zoneinfo 需要 3.9+"),
    (r"^\s*import\s+graphlib", "graphlib 需要 3.9+"),
    (r"\bsys\.orig_argv\b", "sys.orig_argv 需要 3.10+"),
    (r"\bzip\([^)]*strict\s*=", "zip(strict=) 需要 3.10+"),
    (r"\bint\.bit_count\b", "int.bit_count 需要 3.10+"),
    (r"\bdatetime\.UTC\b", "datetime.UTC 需要 3.11+"),
    (r"\bSelf\b\s*[:=]", "typing.Self 需要 3.11+"),
]


def iter_files():
    self_path = os.path.abspath(__file__)
    for base in SCAN_DIRS:
        folder = os.path.join(ROOT, base)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                if os.path.abspath(path) != self_path:  # 本文件里存着这些正则
                    yield path
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py"):
            yield os.path.join(ROOT, name)


def check_syntax(path):
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except Exception as exc:
        return ["读不了文件：%s" % exc], ""
    try:
        ast.parse(source, filename=path, feature_version=TARGET)
    except SyntaxError as exc:
        return ["语法超出 Python %d.%d：第 %s 行 %s" % (TARGET[0], TARGET[1], exc.lineno, exc.msg)], source
    return [], source


def check_api(source, path):
    problems = []
    for pattern, message in FORBIDDEN:
        for match in re.finditer(pattern, source, re.M):
            line = source.count("\n", 0, match.start()) + 1
            problems.append("第 %d 行用了 %s" % (line, message))
    return problems


def main():
    files = list(iter_files())
    failures = 0
    for path in files:
        problems, source = check_syntax(path)
        if source:
            problems.extend(check_api(source, path))
        rel = os.path.relpath(path, ROOT)
        if problems:
            failures += 1
            print("[FAIL] %s" % rel)
            for item in problems:
                print("       - %s" % item)
        else:
            print("[ OK ] %s" % rel)
    print("")
    if failures:
        print("%d 个文件不满足 Python %d.%d 约束。" % (failures, TARGET[0], TARGET[1]))
        return 1
    print("%d 个文件全部满足 Python %d.%d 约束。" % (len(files), TARGET[0], TARGET[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
