# -*- coding: utf-8 -*-
"""一键跑完全部验收：单元/端到端测试 + 兼容性 + 靶场契约自检。

    python tools/verify_all.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(title, args):
    print("")
    print("=" * 70)
    print("  " + title)
    print("=" * 70)
    process = subprocess.Popen(args, cwd=ROOT)
    return process.wait()


def main():
    steps = [
        ("1/4 单元测试 + 端到端（mock 靶场）", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
        ("2/4 Python 3.7 语法兼容检查", [sys.executable, os.path.join("tools", "check_compat.py")]),
        ("3/4 靶场接口契约自检", [sys.executable, os.path.join("tools", "smoke.py")]),
        ("4/4 离线依赖验证（只连本机，不碰外部网络）", [sys.executable, os.path.join("tools", "check_offline.py")]),
    ]
    failures = []
    for title, args in steps:
        if run(title, args) != 0:
            failures.append(title)
    print("")
    print("=" * 70)
    if failures:
        print("  有失败项：")
        for item in failures:
            print("    - " + item)
        print("=" * 70)
        return 1
    print("  全部通过。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
