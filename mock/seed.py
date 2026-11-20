# -*- coding: utf-8 -*-
"""本地靶场用的模拟题库。

不是真实题库，只是把契约需要用到的题型都覆盖一遍：
单选 / 多选 / 判断 / 填空 / 综合题组（内部再含填空与单选），
题干里故意混入全角空格、图片 markdown、全角括号，用来验证归一化匹配。
"""

# 单选：题干、选项、正确答案、知识点
SINGLES = [
    ("下列物体属于实体结构的是（　　）", ["冰山", "脚手架", "高压线铁塔", "自行车车架"], "A", "结构的分类"),
    ("设计师在设计一款学习机时，广泛调查了学生和老师的使用需求，这属于（　　）",
     ["信息收集与整理", "心理测试", "成本核算", "设计的构思"], "A", "发现与明确问题"),
    ("根据我国专利相关法规，下列说法正确的是（　　）",
     ["外观设计专利保护期为15年", "发明专利保护期为10年", "实用新型专利保护期为20年", "专利在申请前必须公开"], "A", "保护创新设计"),
    ("下列关于流程的叙述中，错误的是（　　）",
     ["流程的时序一定不可颠倒", "流程由环节和时序组成", "有些流程的时序可以颠倒", "流程的环节可以合并"], "A", "了解流程"),
    ("在技术活动中，发现问题的途径与方法不包括（　　）",
     ["收集和分析信息", "技术试验", "技术研究", "无目的地观察"], "D", "发现问题"),
    ("![图](images/foldable-ladder.png) 如图所示为一款可折叠梯，其设计主要考虑了（　　）",
     ["结构的稳定性", "结构的强度", "人机关系", "材料的选择"], "C", "人机关系"),
    ("小明想设计一个简易书架，首先要做的是（　　）",
     ["明确问题与设计要求", "绘制加工图", "选择材料", "进行承重测试"], "A", "设计的一般过程"),
    ("下列关于技术试验的说法中，正确的是（　　）",
     ["技术试验是技术活动中必不可少的环节", "技术试验只能用于产品出厂前", "技术试验的结果一定与实际使用一致", "技术试验可以完全替代理论分析"], "A", "技术试验"),
    ("木材加工中用于画线的工具是（　　）",
     ["钢直尺与铅笔", "锯子", "刨子", "凿子"], "A", "木材加工"),
    ("三视图中的主视图，是从物体（　　）方向观察所画的视图",
     ["正前方", "正后方", "正上方", "正左方"], "A", "投影和三视图"),
]

# 多选
MULTIS = [
    ("下列属于技术活动的是（　　）",
     ["设计一款台灯", "论证勾股定理", "改进焊接工艺", "研究天体运动"], "AC", "技术的性质"),
    ("设计一般过程中需要进行的评价有（　　）",
     ["方案评价", "过程评价", "结果评价", "考试评价"], "ABC", "设计评价"),
]

# 判断（选项固定 A 对 / B 错）
JUDGES = [
    ("结构的功能与其形状、材料、连接方式有关。", ["正确", "错误"], "A", "结构的功能"),
    ("流程的时序在任何情况下都不可以颠倒。", ["正确", "错误"], "B", "流程的时序"),
]

# 填空
FILLS = [
    ("防汛报警控制系统中，如果金属导电板失灵，整个系统就不能正常工作，这体现了系统的______（整体性/环境适应性）",
     "整体性", "系统的特性"),
    ("根据我国专利法，实用新型专利的保护期限为______年", "10", "保护创新设计"),
    ("统筹安排工序时，把可以同时进行的工序放在一起，称为______工序（并行/串行）", "并行", "流程的优化"),
    ("尺寸标注中，表示直径的符号是______", "Φ", "机械制图"),
]

# 综合题组：大题干 + 子题（子题可以是填空或单选）
GROUPS = [
    {
        "title": "综合题（一）",
        "stem": "![图](images/flood.png) 如图所示为一款防汛报警控制系统，包括警戒水位检测装置、"
                "电磁铁控制电路和报警电路。水位上涨，浮球上浮。当达到警戒水位时，金属导电板在浮球"
                "推动下上移，使电磁铁控制电路接通，在电磁铁吸力的作用下报警电路的触点开关闭合，报警"
                "电路中的报警器发出报警声。",
        "subs": [
            ("（1）该控制系统中，给定量是______（警戒水位/实际水位）", "fill", None, "警戒水位", "控制系统的组成"),
            ("（2）该控制系统属于（　　）控制系统", "single",
             ["开环", "闭环", "半闭环", "复合"], "A", "控制系统的组成"),
        ],
    },
    {
        "title": "综合题（二）",
        "stem": "某工厂的生产流程包括原料处理、加工、装配、检验四个环节。",
        "subs": [
            ("（1）该流程中最基本的工序是______（加工/检验）", "fill", None, "加工", "流程的分析"),
            ("（2）检验环节在流程中属于（　　）",
             "single", ["输入", "输出", "中间环节", "无关环节"], "B", "流程的分析"),
        ],
    },
]


def _option(key, text):
    return {"key": key, "text": text, "images": []}


def _question(qid, qtype, stem, score, knowledge, answer, options=None, explanation="", images=None):
    return {
        "id": qid,
        "type": qtype,
        "stem": stem,
        "options": options or [],
        "images": images or [],
        "answer": answer,
        "explanation": explanation or ("解析：参考答案 %s。" % answer),
        "knowledge": knowledge,
        "score": score,
        "seq": 0,
        "sub_questions": [],
    }


def build_paper(paper_id, name, question_budget, formal_exam=False, shuffle=False,
                points_enabled=True):
    """按预算拼一套卷：覆盖各题型后用单选补足。"""
    state = {"next": paper_id * 1000 + 100}

    def new_id():
        state["next"] += 1
        return state["next"]

    def make_single(index, score=2.5):
        stem, opts, answer, knowledge = SINGLES[index % len(SINGLES)]
        options = [_option("ABCD"[i], text) for i, text in enumerate(opts)]
        return _question(new_id(), "single", stem, score, knowledge, answer, options)

    def make_multi(index):
        stem, opts, answer, knowledge = MULTIS[index % len(MULTIS)]
        options = [_option("ABCD"[i], text) for i, text in enumerate(opts)]
        return _question(new_id(), "multi", stem, 3.0, knowledge, answer, options)

    def make_judge(index):
        stem, opts, answer, knowledge = JUDGES[index % len(JUDGES)]
        options = [_option("AB"[i], text) for i, text in enumerate(opts)]
        return _question(new_id(), "judge", stem, 2.0, knowledge, answer, options)

    def make_fill(index):
        stem, answer, knowledge = FILLS[index % len(FILLS)]
        return _question(new_id(), "fill", stem, 2.0, knowledge, answer)

    def make_group(index):
        group = GROUPS[index % len(GROUPS)]
        subs = []
        for text, sub_type, opts, answer, knowledge in group["subs"]:
            if sub_type == "fill":
                subs.append(_question(new_id(), "fill", text, 2.0, knowledge, answer))
            else:
                options = [_option("ABCD"[i], text) for i, text in enumerate(opts)]
                subs.append(_question(new_id(), "single", text, 2.0, knowledge, answer, options))
        group_images = [{"src": "images/group-%d.png" % (state["next"] + 1), "caption": "题组示意图"}]
        return {
            "id": new_id(),
            "type": "comprehensive",
            "title": group["title"],
            "stem": group["stem"],
            "options": [],
            "images": group_images,
            "answer": "",
            "explanation": "",
            "knowledge": group["subs"][0][4],
            "score": sum(item["score"] for item in subs),
            "seq": 0,
            "sub_questions": subs,
        }

    factories = [make_single, make_single, make_multi, make_judge, make_fill,
                 make_group, make_single, make_single]
    counts = {"single": 0, "multi": 0, "judge": 0, "fill": 0, "group": 0}
    questions = []
    weight_done = 0
    cursor = 0
    while weight_done < question_budget:
        factory = factories[cursor % len(factories)]
        cursor += 1
        if factory is make_single:
            item = make_single(counts["single"]); counts["single"] += 1; weight = 1
        elif factory is make_multi:
            item = make_multi(counts["multi"]); counts["multi"] += 1; weight = 1
        elif factory is make_judge:
            item = make_judge(counts["judge"]); counts["judge"] += 1; weight = 1
        elif factory is make_fill:
            item = make_fill(counts["fill"]); counts["fill"] += 1; weight = 1
        else:
            item = make_group(counts["group"]); counts["group"] += 1
            weight = len(item["sub_questions"])
        questions.append(item)
        weight_done += weight

    # 统一 seq（与前端"第 N 题"一致，综合题按子题占位）
    counter = 0
    total = 0
    max_score = 0.0
    for item in questions:
        counter += 1
        item["seq"] = counter
        if item["type"] == "comprehensive":
            for sub in item["sub_questions"]:
                counter += 1
                sub["seq"] = counter
                total += 1
                max_score += sub["score"]
        else:
            total += 1
            max_score += item["score"]

    return {
        "id": paper_id,
        "name": name,
        "total": total,
        "max_score": round(max_score, 1),
        "shuffle": bool(shuffle),
        "formal_exam": bool(formal_exam),
        "hidden": False,
        "practice_open": 1,
        "exam_open": 1,
        "points_enabled": bool(points_enabled),
        "points_rules": [
            {"min": 90, "max": 100, "points": 3},
            {"min": 75, "max": 89, "points": 2},
            {"min": 60, "max": 74, "points": 1},
        ],
        "questions": questions,
    }


def build_papers(question_budget=12, paper_count=2, seed=20211119):
    """默认两套卷：2021年B卷（乱序、带积分）、2024年A卷。"""
    names = ["2021年B卷", "2024年A卷", "2022年A卷", "2023年B卷", "2025年A卷"]
    papers = []
    for index in range(max(1, int(paper_count))):
        papers.append(build_paper(
            index + 1,
            names[index % len(names)],
            max(6, int(question_budget)),
            formal_exam=False,
            shuffle=(index % 2 == 0),
            points_enabled=True,
        ))
    return papers


def flatten(question):
    """把综合题摊平成叶子题列表（与前端 slots 口径一致）。"""
    if question.get("type") == "comprehensive" and question.get("sub_questions"):
        return list(question["sub_questions"])
    return [question]


def all_leaves(paper):
    leaves = []
    for question in paper.get("questions") or []:
        leaves.extend(flatten(question))
    return leaves
