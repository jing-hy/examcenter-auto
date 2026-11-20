# -*- coding: utf-8 -*-
"""端到端：对着本地契约服务器跑完整的刷题流程。

    python -m unittest discover -s tests -v
"""

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
from autoexam import matcher                # noqa: E402
from autoexam import netio                  # noqa: E402
from autoexam import solver as solvermod    # noqa: E402
from autoexam import ui                     # noqa: E402
from mock import server as mockserver       # noqa: E402

ACCOUNT = "demo001"
PASSWORD = "123456"


class MockCase(unittest.TestCase):
    """每个用例起一个干净的 mock 服务器。"""

    question_budget = 10
    leak_answers = False

    def setUp(self):
        self.srv, self.thread, self.db = mockserver.start_server(
            port=0, question_budget=self.question_budget, paper_count=2,
            leak_answers=self.leak_answers,
        )
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = tempfile.mkdtemp(prefix="autoexam-")
        self.cfg = configmod.load_config(None, {
            "base_url": self.base,
            "delay_min": 0.01,
            "delay_max": 0.02,
            "poll_interval": 0.02,
            "poll_tries": 3,
            "mode": "exam",
            "fresh": True,
            "submit": True,
            "use_cache": True,
            "save_raw": False,
            "final_check": True,
            "trust_answer_api": "auto",
            "trust_questions_answer": "auto",
            "log_dir": os.path.join(self.tmp, "logs"),
            "raw_dir": os.path.join(self.tmp, "raw"),
            "status_dir": os.path.join(self.tmp, "status"),
            "cache_path": os.path.join(self.tmp, "answer_cache.json"),
        })

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 工具
    def make_stack(self, overrides=None):
        cfg = dict(self.cfg)
        cfg.update(overrides or {})
        logger = ui.Logger(None, echo=False)
        client = netio.HttpClient(cfg["base_url"], timeout=10, retries=2,
                                  save_raw=False, logger=logger)
        api = apimod.ExamCenter(client, logger)
        user = loginmod.login(cfg, client, logger, ACCOUNT, PASSWORD)
        papers = api.papers()
        paper = papers[0]
        cache = matcher.AnswerCache(cfg["cache_path"], True, logger)
        return cfg, logger, client, api, user, paper, cache

    def solve(self, overrides=None):
        cfg, logger, client, api, user, paper, cache = self.make_stack(overrides)
        engine = solvermod.Solver(cfg, api, logger, ACCOUNT, paper, cache)
        return engine.run(), paper


class FullRunTest(MockCase):
    def test_full_run_all_correct(self):
        result, paper = self.solve()
        summary = result["summary"]
        self.assertTrue(result["submitted"], "应当自动交卷")
        self.assertEqual(summary["total"], paper["total"])
        self.assertEqual(summary["correct"], summary["total"],
                         "每题都该拿到正确答案，实际：%s" % (result["counts"],))
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["counts"]["cache"], 0, "首次运行不该命中缓存")
        self.assertGreater(result["counts"]["wrongbook"], 0, "至少有几题要走错题本")
        self.assertAlmostEqual(result["counts"]["cache"], 0)
        # 探针恰好答对的题（unknown）在复核后会被确认并落缓存，不算未解决
        self.assertLessEqual(result["counts"]["unknown"], paper["total"])
        self.assertAlmostEqual(summary["score"], summary["max_score"])

    def test_wrong_book_cleared_after_run(self):
        self.solve()
        _, _, client, api, _, _, _ = self.make_stack()
        self.assertEqual(api.wrong(), [], "全答对后错题本应为空")

    def test_cache_written_and_reused(self):
        first, _ = self.solve()
        learned = first["counts"]["wrongbook"]
        self.assertGreater(learned, 0, "第一次应当通过错题本学到答案")
        second, paper = self.solve()
        self.assertEqual(second["summary"]["correct"], paper["total"])
        self.assertGreaterEqual(second["counts"]["cache"], learned,
                                "第一次学到的答案第二次应当全部走缓存：%s" % (second["counts"],))
        self.assertLess(second["counts"]["cache"], paper["total"] + 1)

    def test_no_submit_keeps_attempt_open(self):
        result, _ = self.solve({"submit": False})
        self.assertFalse(result["submitted"])
        _, _, client, api, _, paper, _ = self.make_stack()
        doing = api.find_paper(paper["id"]).get("doing")
        self.assertIsInstance(doing, dict, "不交卷时应保留未完成进度")
        self.assertGreater(doing.get("answered") or 0, 0)

    def test_points_opt_in_respected(self):
        result, _ = self.solve({"points_opt_in": False})
        summary = result["summary"]
        self.assertEqual(summary.get("points_awarded"), 0)
        self.assertEqual(summary.get("points_count"), 0)

    def test_batch_strategy(self):
        result, paper = self.solve({"strategy": "batch"})
        self.assertEqual(result["summary"]["correct"], paper["total"],
                         "batch 策略也应全对：%s" % (result["counts"],))


class LeakAnswersTest(MockCase):
    """服务端若在考试模式下泄露答案，捷径路径要能用上。"""

    leak_answers = True

    def test_questions_answer_shortcut(self):
        result, paper = self.solve()
        self.assertEqual(result["summary"]["correct"], paper["total"])
        self.assertEqual(result["counts"]["questions"], paper["total"],
                         "题目自带 answer 时应全部走捷径")
        self.assertEqual(result["counts"]["wrongbook"], 0)

    def test_answer_api_shortcut(self):
        # 关掉"题目自带答案"这条，逼它走 /answer 返回值的捷径
        result, paper = self.solve({"trust_questions_answer": "off"})
        self.assertEqual(result["summary"]["correct"], paper["total"])
        self.assertEqual(result["counts"]["answer_api"], paper["total"],
                         "应全部走 /answer 捷径：%s" % (result["counts"],))


class ResumeTest(MockCase):
    def test_fresh_restarts_progress(self):
        self.solve({"submit": False})
        result, paper = self.solve({"fresh": True})
        self.assertEqual(result["summary"]["total"], paper["total"])
        self.assertEqual(result["summary"]["answered"], paper["total"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
