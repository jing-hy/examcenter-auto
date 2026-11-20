# -*- coding: utf-8 -*-
"""错题本分页：/api/wrong 支持 limit/offset（真实服务器实测确认）。

这一组用例守住两件事：
1. api.wrong() 会一直翻页，直到取满为止；条目再多也不会被悄悄截断；
2. 服务端不认 offset 时（每次都回同一页）不能死循环、也不能拿重复条目充数。
"""

import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import api as apimod          # noqa: E402
from autoexam import config as configmod    # noqa: E402
from autoexam import login as loginmod      # noqa: E402
from autoexam import netio                  # noqa: E402
from autoexam import ui                     # noqa: E402
from mock import server as mockserver       # noqa: E402

ACCOUNT = "demo001"
PASSWORD = "123456"


class FakeCenter(apimod.ExamCenter):
    """只有 _req 是真的：用一段内存数据模拟服务端分页。"""

    def __init__(self, rows, ignore_offset=False):
        apimod.ExamCenter.__init__(self, None, None)
        self.rows = list(rows)
        self.ignore_offset = ignore_offset
        self.paths = []

    def _req(self, path, method="GET", body=None, tag=None):
        self.paths.append(path)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        limit = int(query["limit"][0])
        offset = 0 if self.ignore_offset else int(query["offset"][0])
        return {"items": self.rows[offset:offset + limit]}


class PaginationLogicTest(unittest.TestCase):
    def test_collects_every_page(self):
        rows = [{"id": index, "stem": "题%d" % index} for index in range(5)]
        center = FakeCenter(rows)
        items = center.wrong(page_size=2)
        self.assertEqual([item["id"] for item in items], [0, 1, 2, 3, 4])
        self.assertEqual(len(center.paths), 3, "5 条 / 每页 2 条 应该是 3 次请求")

    def test_stops_when_page_not_full(self):
        rows = [{"id": 1}, {"id": 2}]
        center = FakeCenter(rows)
        self.assertEqual(len(center.wrong(page_size=10)), 2)
        self.assertEqual(len(center.paths), 1)

    def test_server_ignoring_offset_does_not_loop_forever(self):
        rows = [{"id": index} for index in range(4)]
        center = FakeCenter(rows, ignore_offset=True)
        items = center.wrong(page_size=2)
        self.assertEqual([item["id"] for item in items], [0, 1],
                         "服务端不认 offset 时只保留第一页，不能重复堆叠")
        self.assertLessEqual(len(center.paths), 3)

    def test_empty_wrong_book(self):
        center = FakeCenter([])
        self.assertEqual(center.wrong(page_size=2), [])
        self.assertEqual(len(center.paths), 1)

    def test_page_size_guard(self):
        center = FakeCenter([{"id": 1}])
        self.assertEqual(len(center.wrong(page_size=0)), 1, "page_size=0 应回落到默认值")


class WrongPaginationMockTest(unittest.TestCase):
    """真服务器（靶场）上的分页：mock 必须和真服务器一样认 limit/offset。"""

    def setUp(self):
        self.srv, self.thread, self.db = mockserver.start_server(
            port=0, question_budget=6, paper_count=1)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = tempfile.mkdtemp(prefix="autoexam-page-")
        self.cfg = configmod.load_config(None, {
            "base_url": self.base,
            "log_dir": os.path.join(self.tmp, "logs"),
            "raw_dir": os.path.join(self.tmp, "raw"),
            "status_dir": os.path.join(self.tmp, "status"),
            "save_raw": False,
            "mode": "exam",
            "fresh": True,
        })
        self.logger = ui.Logger(None, echo=False)
        self.client = netio.HttpClient(self.base, timeout=10, retries=2,
                                       save_raw=False, logger=self.logger)
        self.api = apimod.ExamCenter(self.client, self.logger)
        loginmod.login(self.cfg, self.client, self.logger, ACCOUNT, PASSWORD)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_wrongs(self, count):
        paper = self.api.papers()[0]
        data = self.api.start_attempt(paper["id"], "exam", True, None)
        attempt_id = data["attempt"]["id"]
        made = 0
        for question in data.get("questions") or []:
            if made >= count:
                break
            if question.get("type") not in ("single", "judge"):
                continue
            keys = apimod.option_keys(question)
            if not keys:
                continue
            _, truth = self.db.leaf_index.get(int(question["id"]), (None, None))
            correct = str((truth or {}).get("answer") or "").strip().upper()
            wrong = [key for key in keys if key.upper() not in correct]
            if not wrong:
                continue
            self.api.answer(attempt_id, int(question["id"]), wrong[0])
            made += 1
        return made

    def test_pagination_returns_full_list(self):
        made = self._make_wrongs(3)
        self.assertGreaterEqual(made, 2, "靶场里应该至少能造出 2 条错题")
        whole = self.api.wrong(page_size=1000)
        paged = self.api.wrong(page_size=1)
        self.assertEqual(len(paged), len(whole), "分页结果应与一次取全一致")
        self.assertEqual(sorted(item.get("id") for item in paged),
                         sorted(item.get("id") for item in whole))
        self.assertGreaterEqual(len(whole), made)

    def test_mock_honours_limit_and_offset(self):
        self._make_wrongs(3)
        first = self._get("/plugin/examcenter/api/wrong?limit=1")
        self.assertEqual(len(first), 1, "limit=1 时服务端应只回 1 条")
        page1 = self._get("/plugin/examcenter/api/wrong?limit=2&offset=0")
        page2 = self._get("/plugin/examcenter/api/wrong?limit=2&offset=2")
        ids1 = [item.get("id") for item in page1]
        ids2 = [item.get("id") for item in page2]
        self.assertFalse(set(ids1) & set(ids2), "offset 应当翻页，两页不该重叠")

    def _get(self, path):
        data = self.client.json_request(path)
        return (data or {}).get("items") or []


if __name__ == "__main__":
    unittest.main()
