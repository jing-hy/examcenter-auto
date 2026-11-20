# -*- coding: utf-8 -*-
"""运行环境相关的测试：不依赖外部网络、不被系统代理带偏、连不上时明确失败。"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import config as configmod   # noqa: E402
from autoexam import netio                 # noqa: E402
from autoexam import ui                    # noqa: E402
from mock import server as mockserver      # noqa: E402

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY")


class EnvProxyTest(unittest.TestCase):
    """机器上配了系统/环境代理时，内网请求不能被拐走。"""

    def setUp(self):
        self.saved = {}
        for name in PROXY_VARS:
            self.saved[name] = os.environ.get(name)
        # 指向一个必然连不上的地址：一旦真的走代理，请求就会失败
        for name in PROXY_VARS:
            os.environ[name] = "http://127.0.0.1:1"
        self.srv, self.thread, self.db = mockserver.start_server(port=0, question_budget=6)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.srv.shutdown()
        self.srv.server_close()

    def test_requests_ignore_env_proxy(self):
        client = netio.HttpClient(self.base, timeout=5, retries=1,
                                  logger=ui.Logger(None, echo=False))
        resp = client.request("/login")
        self.assertEqual(resp.status, 200)
        self.assertIn("password", resp.text)

    def test_full_flow_ignores_env_proxy(self):
        tmp = tempfile.mkdtemp(prefix="proxy-")
        try:
            cfg = configmod.load_config(None, {
                "base_url": self.base,
                "paper": 1,
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
            from autoexam import supervisor
            result = supervisor.run_single(cfg, ui.Logger(None, echo=False),
                                           "demo001", "123456", None)
            self.assertIsNotNone(result, "配了假代理时也该能跑通")
            self.assertTrue(result["submitted"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UnreachableServerTest(unittest.TestCase):
    """连不上服务器时要给出可读中文错误，并且返回非 0 退出码。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offline-")
        self.saved_log = configmod.LOG_DIR
        configmod.LOG_DIR = os.path.join(self.tmp, "logs")

    def tearDown(self):
        configmod.LOG_DIR = self.saved_log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_main_returns_nonzero(self):
        from autoexam.__main__ import main
        buffer = io.StringIO()
        argv = ["--base", "http://127.0.0.1:1", "--account", "某人",
                "--paper", "1", "--retries", "1", "--timeout", "2",
                "--no-input-wait", "--no-save-raw"]
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        output = buffer.getvalue()
        self.assertNotEqual(code, 0, "连不上服务器不该返回 0")
        self.assertIn("连不上服务器", output)
        self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
