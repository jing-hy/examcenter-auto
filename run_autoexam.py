# -*- coding: utf-8 -*-
"""入口脚本：双击 / python run_autoexam.py 都用它。

真正的逻辑在 autoexam 包里；这里只负责把项目根目录放进 sys.path，
这样无论从哪个目录调用都能跑起来。
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam.__main__ import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        print("已中断。")
        sys.exit(130)
