# -*- coding: utf-8 -*-
"""Base URL 的自动识别：显式参数 > 网址 txt > 内置候选探活。"""

import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import config as configmod   # noqa: E402


class ProbeTest(unittest.TestCase):
    def _free_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def test_closed_port_is_not_reachable(self):
        self.assertFalse(configmod.probe_base("http://127.0.0.1:1", timeout=0.2))

    def test_open_port_is_reachable(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        try:
            self.assertTrue(configmod.probe_base("http://127.0.0.1:%d" % port, timeout=1.0))
        finally:
            server.close()

    def test_first_reachable_picks_live_one(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        try:
            got = configmod.first_reachable_base([
                "http://127.0.0.1:1",
                "http://127.0.0.1:%d" % port,
            ], timeout=0.5)
            self.assertEqual(got, "http://127.0.0.1:%d" % port)
        finally:
            server.close()

    def test_none_reachable_returns_empty(self):
        self.assertEqual(configmod.first_reachable_base(["http://127.0.0.1:1"], timeout=0.2), "")


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="baseurl-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, overrides):
        cfg = configmod.load_config(os.path.join(self.tmp, "none.json"), overrides)
        return cfg

    def test_explicit_base_wins(self):
        cfg = self._load({"base_url": "http://127.0.0.1:1"})
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:1")
        self.assertEqual(cfg["base_source"], "参数 / config.json")

    def test_txt_wins_over_builtin(self):
        old = configmod.read_url_from_txt
        configmod.read_url_from_txt = lambda root=None: "http://127.0.0.1:9/plugin/examcenter/#/practice/2"
        try:
            cfg = self._load({})
        finally:
            configmod.read_url_from_txt = old
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:9")
        self.assertEqual(cfg["base_source"], "网址文件")

    def test_probe_used_when_no_txt(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        old_read = configmod.read_url_from_txt
        old_candidates = configmod.DEFAULT_BASE_CANDIDATES
        old_timeout = configmod.BASE_PROBE_TIMEOUT
        configmod.read_url_from_txt = lambda root=None: ""
        configmod.DEFAULT_BASE_CANDIDATES = ["http://127.0.0.1:1",
                                             "http://127.0.0.1:%d" % port]
        configmod.BASE_PROBE_TIMEOUT = 0.5
        try:
            cfg = self._load({})
        finally:
            configmod.read_url_from_txt = old_read
            configmod.DEFAULT_BASE_CANDIDATES = old_candidates
            configmod.BASE_PROBE_TIMEOUT = old_timeout
            server.close()
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:%d" % port)
        self.assertEqual(cfg["base_source"], "内置候选地址（已连通）")

    def test_empty_when_nothing_works(self):
        old_read = configmod.read_url_from_txt
        old_candidates = configmod.DEFAULT_BASE_CANDIDATES
        old_timeout = configmod.BASE_PROBE_TIMEOUT
        configmod.read_url_from_txt = lambda root=None: ""
        configmod.DEFAULT_BASE_CANDIDATES = ["http://127.0.0.1:1"]
        configmod.BASE_PROBE_TIMEOUT = 0.2
        try:
            cfg = self._load({})
        finally:
            configmod.read_url_from_txt = old_read
            configmod.DEFAULT_BASE_CANDIDATES = old_candidates
            configmod.BASE_PROBE_TIMEOUT = old_timeout
        self.assertEqual(cfg["base_url"], "")
        self.assertEqual(cfg["base_source"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
