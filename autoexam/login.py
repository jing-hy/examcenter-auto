# -*- coding: utf-8 -*-
"""主系统登录。

考试中心复用主系统账号（core.js:56 会 302 到 /login?next=...），但没有保存
登录页，字段名未知，所以这里做成"自动探测 + 兜底"三段式：

1. 直接给 Cookie（--cookie）：最稳，登录页有验证码时就靠它；
2. 自动解析登录页表单：找含 password 的 form，把账号密码塞进最像的字段；
3. 解析不到就试几个常见的 JSON 登录端点。

成功后一律用 /plugin/examcenter/api/me 探活确认。
"""

import io
import os
import urllib.parse

from . import netio
from . import privacy

BASE = "/plugin/examcenter"
DEFAULT_LOGIN_PATH = "/login?next=/plugin/examcenter/"

try:
    from html.parser import HTMLParser
except ImportError:  # pragma: no cover - Python 2 兜底，不支持
    HTMLParser = None


class LoginError(Exception):
    """登录失败，消息面向使用者。"""


USER_FIELD_HINTS = [
    "username", "user_name", "userid", "user_id", "user", "account", "account_name",
    "login", "loginname", "student_no", "studentno", "studentid", "student_id",
    "stuno", "xh", "sno", "uid", "name", "email", "phone", "mobile", "tel",
    "账号", "学号", "用户名", "工号", "帐号", "用户", "手机",
]
PASS_FIELD_HINTS = ["password", "passwd", "pwd", "pass", "userpass", "pass_word", "密码"]


class _FormCollector(HTMLParser):
    """把页面里的 form 与控件扒出来。"""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._current = {
                "action": attrs.get("action") or "",
                "method": (attrs.get("method") or "get").strip().lower(),
                "enctype": (attrs.get("enctype") or "").strip().lower(),
                "inputs": [],
            }
            self.forms.append(self._current)
            return
        if self._current is None:
            return
        if tag == "input":
            self._current["inputs"].append({
                "tag": "input",
                "name": attrs.get("name") or "",
                "id": attrs.get("id") or "",
                "type": (attrs.get("type") or "text").strip().lower(),
                "value": attrs.get("value") or "",
                "placeholder": attrs.get("placeholder") or "",
                "checked": "checked" in attrs,
            })
        elif tag == "button":
            self._current["inputs"].append({
                "tag": "button",
                "name": attrs.get("name") or "",
                "id": attrs.get("id") or "",
                "type": (attrs.get("type") or "submit").strip().lower(),
                "value": attrs.get("value") or "",
                "placeholder": "",
                "checked": False,
            })
        elif tag in ("select", "textarea"):
            self._current["inputs"].append({
                "tag": tag,
                "name": attrs.get("name") or "",
                "id": attrs.get("id") or "",
                "type": tag,
                "value": "",
                "placeholder": "",
                "checked": False,
            })

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def parse_forms(html):
    """解析出所有表单；解析不了返回空列表。"""
    if not HTMLParser or not html:
        return []
    collector = _FormCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:
        return collector.forms
    return collector.forms


def _score(text, hints):
    text = (text or "").lower()
    if not text:
        return 0
    best = 0
    for index, hint in enumerate(hints):
        if hint in text:
            best = max(best, 100 - index)
    return best


def pick_login_form(forms):
    """挑最像登录表单的那个（必须含 password 输入）。"""
    best = None
    best_score = -1
    for form in forms:
        has_password = any(item["type"] == "password" and item["tag"] == "input"
                           for item in form["inputs"])
        if not has_password:
            continue
        score = 10
        for item in form["inputs"]:
            if item["type"] == "password":
                score += 50
            elif item["type"] in ("text", "email", "tel", ""):
                score += 20
                if _score(item["name"] + item["id"] + item["placeholder"], USER_FIELD_HINTS):
                    score += 30
        if score > best_score:
            best, best_score = form, score
    return best


def guess_fields(form, user_override="", pass_override=""):
    """返回 (用户名字段名, 密码字段名)。"""
    user_field = user_override or ""
    pass_field = pass_override or ""
    if not pass_field:
        for item in form["inputs"]:
            if item["type"] == "password":
                pass_field = item["name"] or item["id"]
                break
    if not user_field:
        best_score = -1
        candidates = [item for item in form["inputs"]
                      if item["tag"] == "input" and item["type"] in ("text", "email", "tel", "")]
        for item in candidates:
            token = " ".join([item["name"], item["id"], item["placeholder"]])
            score = _score(token, USER_FIELD_HINTS)
            if score > best_score:
                best_score, user_field = score, (item["name"] or item["id"])
        if not user_field and len(candidates) == 1:
            user_field = candidates[0]["name"] or candidates[0]["id"]
        if not user_field and candidates:
            user_field = candidates[0]["name"] or candidates[0]["id"]
    return user_field, pass_field


def build_form_fields(form, user_field, pass_field, username, password):
    """把表单所有控件填好（hidden/checkbox 原样带上）。"""
    fields = []
    for item in form["inputs"]:
        if item["tag"] == "button" and item["type"] not in ("submit", "image"):
            continue
        name = item["name"]
        if not name:
            continue
        low = name.lower()
        if item["type"] == "password" or name == pass_field or low in PASS_FIELD_HINTS:
            fields.append((name, password))
        elif name == user_field:
            fields.append((name, username))
        elif item["type"] == "checkbox":
            if item["checked"] or low in ("remember", "rememberme", "remember_me"):
                fields.append((name, item["value"] or "1"))
        elif item["type"] in ("submit", "image"):
            fields.append((name, item["value"] or ""))
        else:
            fields.append((name, item["value"] or ""))
    return fields


def try_me(client):
    """探活：返回 user dict 或 None。"""
    try:
        data = client.json_request(BASE + "/api/me")
    except netio.Unauthorized:
        return None
    except netio.ApiError:
        return None
    except netio.NetworkError:
        raise
    if isinstance(data, dict):
        user = data.get("user")
        if isinstance(user, dict):
            return user
    return None


def _dump_html(cfg, username, html):
    """保存登录页快照，仅用于排查表单字段名。

    默认不保存（登录页里可能带会话痕迹）；只有显式 --save-raw 时才落盘，
    文件名用匿名 id，不含账号。
    """
    if not cfg.get("save_raw"):
        return ""
    try:
        folder = cfg.get("log_dir") or "logs"
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        path = os.path.join(folder, "login-%s.html" % privacy.slug(username))
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(html or "")
        return path
    except Exception:
        return ""


def _diagnose(html, status, url):
    """从失败页面里找点线索，拼成一句人话。"""
    hints = []
    text = html or ""
    for keyword, tip in (
        ("验证码", "页面出现'验证码'，需要人工登录后拿 Cookie"),
        ("captcha", "页面出现 captcha，需要人工登录后拿 Cookie"),
        ("密码错误", "页面提示密码错误"),
        ("密码不正确", "页面提示密码不正确"),
        ("用户不存在", "页面提示用户不存在"),
        ("账号不存在", "页面提示账号不存在"),
        ("错误", "页面含错误提示"),
        ("error", "页面含 error 提示"),
    ):
        if keyword in text:
            hints.append(tip)
    if status and status >= 400:
        hints.append("登录请求返回 HTTP %s" % status)
    if url:
        hints.append("最终地址 %s" % url)
    return "；".join(hints)


def login(cfg, client, logger, username, password=""):
    """登录并返回 user dict；失败抛 LoginError。"""
    username = (username or "").strip()
    if not username:
        raise LoginError("账号为空")
    password = password or cfg.get("password") or "123456"

    # ---- 通道 1：直接给 Cookie
    cookie = cfg.get("cookie") or ""
    if cookie:
        count = client.import_cookies(cookie)
        logger.info("使用命令行提供的 Cookie（%d 项）" % count)
        user = try_me(client)
        if user:
            return _finish(user, username, logger)
        raise LoginError("提供的 Cookie 无效或已过期（/api/me 未通过）")

    # ---- 通道 2：已经有会话（比如同一进程复用了 client）
    user = try_me(client)
    if user:
        if _same_user(user, username):
            return _finish(user, username, logger)
        logger.debug("检测到旧会话属于 %s，清除后重新登录" % user.get("username"))
        client.cookies.clear()

    # ---- 通道 3：解析登录页表单
    login_path = cfg.get("login_url") or DEFAULT_LOGIN_PATH
    resp = client.request(login_path, tag="login-page")
    html = resp.text
    saved = _dump_html(cfg, username, html)
    if saved:
        logger.debug("登录页已保存：%s" % saved)
    if resp.status >= 400:
        raise LoginError("打不开登录页（HTTP %s）：%s" % (resp.status, login_path))

    forms = parse_forms(html)
    form = pick_login_form(forms)
    if form:
        user_field, pass_field = guess_fields(
            form, cfg.get("login_user_field") or "", cfg.get("login_pass_field") or "")
        if not user_field or not pass_field:
            raise LoginError("登录表单里找不到账号/密码字段，请用 --cookie 兜底")
        fields = build_form_fields(form, user_field, pass_field, username, password)
        action = form.get("action") or ""
        if not action or action.lower().startswith("javascript"):
            action = login_path.split("?")[0]
        logger.info("表单登录：字段 %s / %s" % (user_field, pass_field))
        if form.get("method") == "get":
            query = urllib.parse.urlencode(fields)
            resp = client.request("%s?%s" % (action, query), tag="login-submit")
        elif "multipart" in (form.get("enctype") or ""):
            resp = client.request(action, method="POST", files=fields, tag="login-submit")
        else:
            # 不跟随重定向：登录成功/失败通常靠 302 的 Location 区分，
            # 而且 302 响应里的 Set-Cookie 必须先吸收掉。
            resp = client.request(action, method="POST", form_body=fields,
                                  tag="login-submit", allow_redirect=False)
            if resp.status in (301, 302, 303, 307, 308):
                location = ""
                try:
                    location = resp.headers.get("Location") or ""
                except Exception:
                    location = ""
                logger.debug("登录请求返回 %s，跳转到 %s" % (resp.status, location or "（空）"))
                if location:
                    # 跟着跳一次，顺便把落地页留下当失败诊断的素材
                    resp = client.request(location, tag="login-follow")
        user = try_me(client)
        if user:
            return _finish(user, username, logger)
        detail = _diagnose(resp.text, resp.status, resp.url)
        logger.warn("表单登录未通过。" + (detail or "服务端没有报错提示"))

    # ---- 通道 4：猜 JSON 登录端点
    payloads = [
        {"username": username, "password": password},
        {"user": username, "password": password},
        {"account": username, "password": password},
        {"username": username, "password": password, "remember": True},
    ]
    for path in ("/api/login", "/login", "/doLogin", "/api/auth/login"):
        for payload in payloads:
            try:
                client.request(path, method="POST", json_body=payload, tag="login-json")
            except netio.NetworkError:
                raise
            except Exception:
                continue
            user = try_me(client)
            if user:
                logger.info("JSON 登录成功：POST %s" % path)
                return _finish(user, username, logger)

    raise LoginError(
        "登录失败：自动探测没能通过。\n"
        "  下一步：在浏览器里手工登录考试中心，F12 → Network → 任意请求的 Cookie，\n"
        "  然后带上参数重跑：--cookie \"SESSION=xxx; other=yyy\"\n"
        "  登录页快照已保存在 logs/ 下，可用于补全字段名。"
    )


def _same_user(user, username):
    remote = str(user.get("username") or user.get("name") or "").strip()
    if not remote:
        return True
    return remote == username or remote == username.lstrip("0")


def _finish(user, username, logger):
    # 先登记姓名，保证下面这行"登录成功：姓名（账号）"落盘时已经脱敏
    if hasattr(logger, "add_secret"):
        logger.add_secret(user.get("name"), user.get("username"))
    if user.get("must_change"):
        raise LoginError(
            "账号 %s 被要求先修改密码（must_change），本程序不会自动改密码。\n"
            "  请先用浏览器登录一次并改好密码，再回来运行。" % username
        )
    role = user.get("role") or "student"
    if role != "student":
        logger.warn("当前账号角色是 %s，学生端错题本接口可能不可用" % role)
    if not _same_user(user, username):
        logger.warn("登录后的账号（%s）与输入（%s）不一致，请确认" %
                    (user.get("username"), username))
    logger.ok("登录成功：%s（%s）" % (user.get("name") or username,
                                   user.get("username") or username))
    return user
