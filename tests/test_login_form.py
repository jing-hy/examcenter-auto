# -*- coding: utf-8 -*-
"""登录表单解析与登录流程的测试。"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import config as configmod   # noqa: E402
from autoexam import login as loginmod     # noqa: E402
from autoexam import netio                 # noqa: E402
from autoexam import ui                    # noqa: E402
from mock import server as mockserver      # noqa: E402

ACCOUNT = "demo001"
PASSWORD = "123456"

FORM_HTML = u"""
<html><body>
<form method="post" action="/sso/login">
  <input type="hidden" name="csrf_token" value="abc123">
  <input type="hidden" name="next" value="/plugin/examcenter/">
  <label>学号</label><input type="text" name="xh" id="xh" placeholder="请输入学号">
  <label>密码</label><input type="password" name="mm" id="mm">
  <label><input type="checkbox" name="remember" value="1" checked> 记住</label>
  <button type="submit" name="doLogin" value="1">登录</button>
</form>
<form method="get" action="/search">
  <input type="text" name="keyword">
</form>
</body></html>
"""


class ParseFormTest(unittest.TestCase):
    def test_finds_forms_and_inputs(self):
        forms = loginmod.parse_forms(FORM_HTML)
        self.assertEqual(len(forms), 2)
        self.assertEqual(forms[0]["action"], "/sso/login")
        self.assertEqual(forms[0]["method"], "post")

    def test_picks_form_with_password(self):
        form = loginmod.pick_login_form(loginmod.parse_forms(FORM_HTML))
        self.assertIsNotNone(form)
        self.assertEqual(form["action"], "/sso/login")

    def test_guess_fields_by_placeholder_and_type(self):
        form = loginmod.pick_login_form(loginmod.parse_forms(FORM_HTML))
        user_field, pass_field = loginmod.guess_fields(form)
        self.assertEqual(user_field, "xh")
        self.assertEqual(pass_field, "mm")

    def test_guess_fields_common_naming_styles(self):
        cases = [
            (u'<form method="post"><input type="text" name="username">'
             u'<input type="password" name="password"></form>', "username", "password"),
            (u'<form method="post"><input type="text" name="account">'
             u'<input type="password" name="pwd"></form>', "account", "pwd"),
            (u'<form method="post"><input type="text" placeholder="请输入账号" name="a1">'
             u'<input type="password" name="p1"></form>', "a1", "p1"),
            (u'<form method="post"><input type="email" name="email">'
             u'<input type="password" name="pass"></form>', "email", "pass"),
            (u'<form method="post"><input type="text" name="only">'
             u'<input type="password" name="p"></form>', "only", "p"),
        ]
        for html, expect_user, expect_pass in cases:
            form = loginmod.pick_login_form(loginmod.parse_forms(html))
            user_field, pass_field = loginmod.guess_fields(form)
            self.assertEqual((user_field, pass_field), (expect_user, expect_pass), html)

    def test_override_fields(self):
        form = loginmod.pick_login_form(loginmod.parse_forms(FORM_HTML))
        user_field, pass_field = loginmod.guess_fields(form, "xh", "mm")
        self.assertEqual((user_field, pass_field), ("xh", "mm"))

    def test_build_fields_keeps_hidden_and_checkbox(self):
        form = loginmod.pick_login_form(loginmod.parse_forms(FORM_HTML))
        fields = dict(loginmod.build_form_fields(form, "xh", "mm", ACCOUNT, PASSWORD))
        self.assertEqual(fields["csrf_token"], "abc123")
        self.assertEqual(fields["next"], "/plugin/examcenter/")
        self.assertEqual(fields["xh"], ACCOUNT)
        self.assertEqual(fields["mm"], PASSWORD)
        self.assertEqual(fields.get("remember"), "1")
        self.assertEqual(fields.get("doLogin"), "1")


class LoginFlowTest(unittest.TestCase):
    def setUp(self):
        self.srv, self.thread, self.db = mockserver.start_server(port=0, question_budget=6)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = tempfile.mkdtemp(prefix="login-")
        self.cfg = configmod.load_config(None, {
            "base_url": self.base,
            "log_dir": os.path.join(self.tmp, "logs"),
            "raw_dir": os.path.join(self.tmp, "raw"),
            "status_dir": os.path.join(self.tmp, "status"),
            "cache_path": os.path.join(self.tmp, "cache.json"),
            "save_raw": False,
        })

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, cfg=None):
        cfg = cfg or self.cfg
        return netio.HttpClient(cfg["base_url"], timeout=10, retries=2,
                                save_raw=False, logger=ui.Logger(None, echo=False))

    def test_form_login(self):
        logger = ui.Logger(None, echo=False)
        client = self._client()
        user = loginmod.login(self.cfg, client, logger, ACCOUNT, PASSWORD)
        self.assertEqual(user["username"], ACCOUNT)
        self.assertEqual(user["name"], "测试用户")
        self.assertIn("sid", client.cookies)

    def test_login_page_snapshot_not_saved_by_default(self):
        """默认不把登录页落盘：快照里可能带会话痕迹。"""
        logger = ui.Logger(None, echo=False)
        loginmod.login(self.cfg, self._client(), logger, ACCOUNT, PASSWORD)
        folder = os.path.join(self.tmp, "logs")
        names = os.listdir(folder) if os.path.isdir(folder) else []
        self.assertFalse(any(name.startswith("login-") for name in names), names)

    def test_login_page_snapshot_when_save_raw(self):
        """显式 --save-raw 时才落盘，且文件名里不含账号。"""
        cfg = dict(self.cfg)
        cfg["save_raw"] = True
        logger = ui.Logger(None, echo=False)
        loginmod.login(cfg, self._client(cfg), logger, ACCOUNT, PASSWORD)
        logs = os.listdir(os.path.join(self.tmp, "logs"))
        self.assertTrue(any(name.startswith("login-") for name in logs), logs)
        self.assertFalse(any(ACCOUNT in name for name in logs), logs)

    def test_wrong_password_raises(self):
        logger = ui.Logger(None, echo=False)
        with self.assertRaises(loginmod.LoginError):
            loginmod.login(self.cfg, self._client(), logger, ACCOUNT, "bad-password")

    def test_cookie_channel(self):
        logger = ui.Logger(None, echo=False)
        client = self._client()
        loginmod.login(self.cfg, client, logger, ACCOUNT, PASSWORD)
        cookie = client.cookie_header()

        cfg = dict(self.cfg)
        cfg["cookie"] = cookie
        fresh = self._client(cfg)
        user = loginmod.login(cfg, fresh, ui.Logger(None, echo=False), ACCOUNT, "")
        self.assertEqual(user["username"], ACCOUNT)

    def test_bad_cookie_raises(self):
        cfg = dict(self.cfg)
        cfg["cookie"] = "sid=definitely-not-valid"
        with self.assertRaises(loginmod.LoginError):
            loginmod.login(cfg, self._client(cfg), ui.Logger(None, echo=False), ACCOUNT, PASSWORD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
