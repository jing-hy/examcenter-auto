# -*- coding: utf-8 -*-
"""匹配与缓存的单元测试。"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autoexam import matcher  # noqa: E402


class NormTextTest(unittest.TestCase):
    def test_fullwidth_and_space_are_equivalent(self):
        a = matcher.norm_text("下列物体属于实体结构的是（　　）")
        b = matcher.norm_text("下列物体属于实体结构的是(  )")
        self.assertEqual(a, b)

    def test_image_markdown_becomes_placeholder(self):
        a = matcher.norm_text("![图](images/x.png) 如图所示为一款可折叠梯")
        b = matcher.norm_text("[图] 如图所示为一款可折叠梯")
        self.assertEqual(a, b)
        self.assertIn("图", a)

    def test_html_img_is_placeholder_too(self):
        a = matcher.norm_text('<img class="q-img" src="a.png">题干')
        b = matcher.norm_text("题干")
        self.assertNotEqual(a, b)  # 图片位置是有效信息，不能直接丢掉
        self.assertTrue(a.startswith("图"))

    def test_hash_is_stable_across_variants(self):
        one = "防汛报警控制系统中，如果金属导电板失灵，这体现了系统的______（整体性/环境适应性）"
        two = "防汛报警控制系统中,如果金属导电板失灵,这体现了系统的______(整体性/环境适应性)"
        self.assertEqual(matcher.stem_hash(one), matcher.stem_hash(two))

    def test_different_stems_differ(self):
        self.assertNotEqual(matcher.stem_hash("甲题"), matcher.stem_hash("乙题"))


class WrongBookIndexTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"id": 1, "paper_name": "2021年B卷", "paper_id": 1,
             "stem": "下列物体属于实体结构的是（　　）", "answer": "A", "seq": 1},
            {"id": 2, "paper_name": "2021年B卷", "paper_id": 1,
             "stem": "根据我国专利相关法规，下列说法正确的是（　　）", "answer": "A", "seq": 5},
            {"id": 3, "paper_name": "2024年A卷", "paper_id": 2,
             "stem": "下列物体属于实体结构的是（　　）", "answer": "B", "seq": 2},
        ]
        self.index = matcher.WrongBookIndex(self.items)

    def test_exact_hit(self):
        result = self.index.find("2021年B卷", "下列物体属于实体结构的是(  )")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["answer"], "A")
        self.assertEqual(result["via"], "exact")

    def test_paper_name_separates_answers(self):
        """同题干出现在两套卷里，必须按套卷取各自的答案。"""
        result = self.index.find("2024年A卷", "下列物体属于实体结构的是（　　）")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["answer"], "B")

    def test_unknown_paper_is_missing(self):
        result = self.index.find("2022年C卷", "下列物体属于实体结构的是（　　）")
        self.assertEqual(result["status"], "missing")

    def test_unknown_stem_is_missing(self):
        result = self.index.find("2021年B卷", "这道题完全没见过")
        self.assertEqual(result["status"], "missing")

    def test_ambiguous_answers_rejected(self):
        index = matcher.WrongBookIndex([
            {"id": 1, "paper_name": "卷A", "stem": "同一道题", "answer": "A"},
            {"id": 2, "paper_name": "卷A", "stem": "同一道题", "answer": "C"},
        ])
        result = index.find("卷A", "同一道题")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["answers"], ["A", "C"])

    def test_same_answer_candidates_are_fine(self):
        index = matcher.WrongBookIndex([
            {"id": 1, "paper_name": "卷A", "stem": "同一道题", "answer": "A"},
            {"id": 2, "paper_name": "卷A", "stem": "同一道题", "answer": "A"},
        ])
        result = index.find("卷A", "同一道题")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["answer"], "A")

    def test_prefix_fallback(self):
        long_stem = "防汛报警控制系统中，如果金属导电板失灵了，必须维修或更换，否则该系统就不能正常工作，这体现了系统的______"
        index = matcher.WrongBookIndex([
            {"id": 9, "paper_name": "卷A", "stem": long_stem, "answer": "整体性"},
        ])
        # 尾部被截断（模拟两端取到的文本略有出入）
        result = index.find("卷A", long_stem[:32])
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["answer"], "整体性")
        self.assertEqual(result["via"], "prefix")


class AnswerSaneTest(unittest.TestCase):
    def test_choice_answer_must_be_in_options(self):
        question = {"type": "single", "options": [{"key": "A"}, {"key": "B"}]}
        self.assertTrue(matcher.answer_is_sane(question, "A"))
        self.assertTrue(matcher.answer_is_sane(question, "AB"))
        self.assertFalse(matcher.answer_is_sane(question, "D"))
        self.assertFalse(matcher.answer_is_sane(question, ""))

    def test_multi_accepts_multiple_letters(self):
        question = {"type": "multi", "options": [{"key": k} for k in "ABCD"]}
        self.assertTrue(matcher.answer_is_sane(question, "ACD"))
        self.assertFalse(matcher.answer_is_sane(question, "ACE"))

    def test_fill_accepts_any_non_empty(self):
        question = {"type": "fill", "options": []}
        self.assertTrue(matcher.answer_is_sane(question, "整体性"))
        self.assertFalse(matcher.answer_is_sane(question, ""))


class AnswerCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cache-")
        self.path = os.path.join(self.tmp, "answer_cache.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_and_get_survives_reload(self):
        cache = matcher.AnswerCache(self.path, True)
        cache.put(1, "2021年B卷", "某题干", "A", "wrongbook")
        again = matcher.AnswerCache(self.path, True)
        entry = again.get(1, "某题干")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["answer"], "A")
        self.assertEqual(entry["source"], "wrongbook")

    def test_key_includes_paper_id(self):
        cache = matcher.AnswerCache(self.path, True)
        cache.put(1, "卷A", "同一道题", "A", "wrongbook")
        self.assertIsNone(cache.get(2, "同一道题"))

    def test_empty_answer_is_not_cached(self):
        cache = matcher.AnswerCache(self.path, True)
        self.assertFalse(cache.put(1, "卷A", "某题干", "", "wrongbook"))
        self.assertIsNone(cache.get(1, "某题干"))

    def test_disabled_cache_is_noop(self):
        cache = matcher.AnswerCache(self.path, False)
        cache.put(1, "卷A", "某题干", "A", "wrongbook")
        self.assertIsNone(cache.get(1, "某题干"))

    def test_merges_concurrent_writers(self):
        one = matcher.AnswerCache(self.path, True)
        two = matcher.AnswerCache(self.path, True)
        one.put(1, "卷A", "题一", "A", "wrongbook")
        two.put(1, "卷A", "题二", "B", "wrongbook")
        final = matcher.AnswerCache(self.path, True)
        self.assertIsNotNone(final.get(1, "题一"))
        self.assertIsNotNone(final.get(1, "题二"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
