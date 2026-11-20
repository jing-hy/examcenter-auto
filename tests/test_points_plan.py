# -*- coding: utf-8 -*-
"""「全部积分」模式 + 落盘无个人信息的自动化验证。"""

import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import api as apimod          # noqa: E402
from autoexam import config as configmod    # noqa: E402
from autoexam import login as loginmod      # noqa: E402
from autoexam import netio                  # noqa: E402
from autoexam import supervisor             # noqa: E402
from autoexam import ui                     # noqa: E402
from autoexam import worker as workermod    # noqa: E402
from mock import server as mockserver       # noqa: E402

ACCOUNT = "demo001"
PASSWORD = "123456"
DISPLAY_NAME = "测试用户"


class PlanCase(unittest.TestCase):
    question_budget = 6
    paper_count = 3

    def setUp(self):
        self.srv, self.thread, self.db = mockserver.start_server(
            port=0, question_budget=self.question_budget, paper_count=self.paper_count)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = tempfile.mkdtemp(prefix="points-")
        self.cfg = configmod.load_config(None, {
            "base_url": self.base,
            "delay_min": 0.0,
            "delay_max": 0.0,
            "poll_interval": 0.02,
            "poll_tries": 3,
            "mode": "exam",
            "fresh": True,
            "submit": True,
            "use_cache": True,
            "save_raw": False,
            "final_check": True,
            "log_dir": os.path.join(self.tmp, "logs"),
            "raw_dir": os.path.join(self.tmp, "raw"),
            "status_dir": os.path.join(self.tmp, "status"),
            "cache_path": os.path.join(self.tmp, "answer_cache.json"),
        })

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _api(self):
        logger = ui.Logger(None, echo=False)
        client = netio.HttpClient(self.cfg["base_url"], timeout=10, retries=2, logger=logger)
        api = apimod.ExamCenter(client, logger)
        loginmod.login(self.cfg, client, logger, ACCOUNT, PASSWORD)
        return api


class PointsLineTest(PlanCase):
    def test_paper_line_shows_remaining(self):
        api = self._api()
        line = apimod.paper_line(api.papers()[0])
        self.assertIn("还可计分3次", line, line)
        self.assertIn("积分0/3", line, line)

    def test_points_remaining(self):
        self.assertEqual(apimod.points_remaining({"points_enabled": False}), 0)
        self.assertEqual(apimod.points_remaining({"points_enabled": True, "points_awards": 0}), 3)
        self.assertEqual(apimod.points_remaining({"points_enabled": True, "points_awards": 2}), 1)
        self.assertEqual(apimod.points_remaining({"points_enabled": True, "points_awards": 3}), 0)


class PlanBuildTest(PlanCase):
    def test_plan_expands_each_paper_by_remaining_times(self):
        api = self._api()
        papers = api.papers()
        plan = supervisor.build_points_plan(papers, ui.Logger(None, echo=False))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["_kind"], "points-plan")
        self.assertEqual(len(plan["tasks"]), len(papers))
        for task in plan["tasks"]:
            self.assertEqual(task["times"], 3)

    def test_plan_skips_closed_exam(self):
        logger = ui.Logger(None, echo=False)
        papers = [
            {"id": 1, "name": "开着的", "points_enabled": True, "points_awards": 1, "exam_open": 1},
            {"id": 2, "name": "关着的", "points_enabled": True, "points_awards": 0, "exam_open": 0},
            {"id": 3, "name": "没积分", "points_enabled": False, "points_awards": 0, "exam_open": 1},
            {"id": 4, "name": "已满", "points_enabled": True, "points_awards": 3, "exam_open": 1},
        ]
        plan = supervisor.build_points_plan(papers, logger)
        self.assertEqual([t["paper"]["id"] for t in plan["tasks"]], [1])
        self.assertEqual(plan["tasks"][0]["times"], 2)

    def test_plan_none_when_everything_full(self):
        logger = ui.Logger(None, echo=False)
        papers = [{"id": 1, "name": "满", "points_enabled": True,
                   "points_awards": 3, "exam_open": 1}]
        self.assertIsNone(supervisor.build_points_plan(papers, logger))


class PlanRunTest(PlanCase):
    question_budget = 4
    paper_count = 2

    def test_worker_runs_until_all_points_full(self):
        api = self._api()
        plan = supervisor.build_points_plan(api.papers(), ui.Logger(None, echo=False))
        self.assertEqual(len(plan["tasks"]), 2)

        result = workermod.run(self.cfg, ACCOUNT, PASSWORD, plan)

        self.assertEqual(len(result["rounds"]), 6, "2 套卷 × 3 次 = 6 轮")
        self.assertTrue(result["submitted"])
        summary = result["summary"]
        self.assertEqual(summary["points_count"], 6)
        self.assertGreater(summary["points_awarded"], 0)

        api = self._api()
        for paper in api.papers():
            self.assertEqual(paper["points_awards"], 3, "每套卷都应刷满 3 次积分")

    def test_no_personal_info_written_to_disk(self):
        """做题全过程中，磁盘上任何文件都不应出现账号、密码、姓名。"""
        api = self._api()
        plan = supervisor.build_points_plan(api.papers(), ui.Logger(None, echo=False))
        workermod.run(self.cfg, ACCOUNT, PASSWORD, plan)

        needle = [ACCOUNT, PASSWORD, DISPLAY_NAME]
        offenders = []
        for dirpath, _, filenames in os.walk(self.tmp):
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, self.tmp)
                for word in needle:
                    if word in name:
                        offenders.append("%s（文件名含 %s）" % (rel, word))
                try:
                    with io.open(path, "rb") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                text = blob.decode("utf-8", "replace")
                for word in needle:
                    if word in text:
                        offenders.append("%s（内容含 %s）" % (rel, word))
        self.assertEqual(offenders, [], "落盘内容泄露了个人信息：%s" % offenders)

    def test_job_payload_has_no_account_or_password(self):
        account = {"username": ACCOUNT, "password": PASSWORD}
        paper = {"id": 1, "name": "x"}
        payload = supervisor.job_payload(self.cfg, account, paper)
        blob = repr(payload)
        self.assertNotIn(ACCOUNT, blob)
        self.assertNotIn(PASSWORD, blob)
        self.assertEqual(payload["account_slug"], "acc-3d3bf00b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
