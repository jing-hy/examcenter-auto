# -*- coding: utf-8 -*-
"""HTTP 基础设施：Cookie、请求、重试、原始响应落盘。

只依赖标准库。cookie 自己维护（不用 http.cookiejar），原因：
1. 需要把浏览器里拷来的 Cookie 字符串直接导入（--cookie 兜底通道）；
2. 需要被多线程共享而不出竞态，自己加锁最直观；
3. 只关心 session cookie，不需要域/路径/过期这套复杂规则。
"""

import io
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class NetworkError(Exception):
    """连不上 / 超时 / 连接被重置。"""


class ApiError(Exception):
    """服务端返回 4xx/5xx。"""

    def __init__(self, status, message, body=None, url=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.body = body
        self.url = url

    def __str__(self):
        return "[HTTP %s] %s" % (self.status, self.message)


class Unauthorized(ApiError):
    """未登录或会话失效。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _CookieAwareRedirect(urllib.request.HTTPRedirectHandler):
    """跟随重定向时顺手吸收 Set-Cookie。

    登录几乎都是 "POST /login → 302 + Set-Cookie → GET 首页"，
    而 urllib 的默认重定向处理器会把中间响应整个丢掉，cookie 就丢了。
    真实系统上这个坑必踩，所以在这里堵住。
    """

    def __init__(self, absorb):
        urllib.request.HTTPRedirectHandler.__init__(self)
        self._absorb = absorb

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            self._absorb(headers)
        except Exception:
            pass
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


class Response(object):
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    @property
    def text(self):
        charset = self.charset()
        try:
            return self.body.decode(charset, "replace")
        except Exception:
            return self.body.decode("utf-8", "replace")

    def charset(self):
        try:
            ctype = self.headers.get("Content-Type") or ""
        except Exception:
            ctype = ""
        low = ctype.lower()
        if "charset=" in low:
            return low.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return "utf-8"

    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.text)
        except Exception:
            return None


def normalize_base(base_url):
    """把 base_url 归一成 scheme://host[:port]（去掉末尾斜杠、路径与 hash）。"""
    base_url = (base_url or "").strip()
    if not base_url:
        return ""
    if "://" not in base_url:
        base_url = "http://" + base_url
    parts = urllib.parse.urlsplit(base_url)
    netloc = parts.netloc or parts.path
    return "%s://%s" % (parts.scheme, netloc.rstrip("/"))


def multipart_body(fields, boundary=None):
    """构造 multipart/form-data 请求体（登录表单 enctype 用得到）。"""
    boundary = boundary or ("----autoexam%s" % int(time.time() * 1000))
    chunks = []
    for name, value in fields:
        chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
        chunks.append(
            ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8")
        )
        chunks.append((u"" if value is None else u"%s" % value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


class HttpClient(object):
    """一个账号一个实例；内部加锁，可被心跳线程共享。"""

    def __init__(
        self,
        base_url,
        timeout=15,
        retries=3,
        save_raw=False,
        raw_dir="logs/raw",
        logger=None,
        tag_prefix="",
        insecure_tls=False,
        user_agent=None,
        proxy=None,
    ):
        self.base_url = normalize_base(base_url)
        self.timeout = float(timeout or 15)
        self.retries = max(1, int(retries or 1))
        self.save_raw = bool(save_raw)
        self.raw_dir = raw_dir
        self.logger = logger
        self.tag_prefix = tag_prefix or ""
        self.user_agent = user_agent or DEFAULT_UA
        self.cookies = {}
        self.referer = self.base_url + "/plugin/examcenter/"
        self._lock = threading.RLock()
        self._seq = 0
        handlers = []
        if self.base_url.lower().startswith("https"):
            try:
                ctx = ssl.create_default_context()
                if insecure_tls:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                handlers.append(urllib.request.HTTPSHandler(context=ctx))
            except Exception:
                pass
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            # 显式禁用环境变量代理（http_proxy/https_proxy）。
            # 不加这一条，urllib 默认会读系统/环境变量代理，把你的内网请求
            # 拐到外网代理上去——那样就变成了"依赖外部网络"。
            # 确实需要代理时用 --proxy 显式给。
            handlers.append(urllib.request.ProxyHandler({}))
        redirect_handler = _CookieAwareRedirect(self._absorb_cookies)
        self._opener = urllib.request.build_opener(redirect_handler, *handlers)
        self._opener_nr = urllib.request.build_opener(_NoRedirect(), *handlers)

    # ------------------------------------------------------------------ cookie
    def cookie_header(self):
        with self._lock:
            items = ["%s=%s" % (k, v) for k, v in self.cookies.items()]
        return "; ".join(items)

    def import_cookies(self, text):
        """导入 'k=v; k2=v2' 形式的 Cookie 字符串，返回导入条数。"""
        count = 0
        for chunk in str(text or "").replace("\n", ";").split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name = name.strip()
            if not name:
                continue
            with self._lock:
                self.cookies[name] = value.strip()
            count += 1
        return count

    def _absorb_cookies(self, headers):
        if headers is None:
            return
        try:
            raws = headers.get_all("Set-Cookie") or []
        except Exception:
            raws = []
        for raw in raws:
            first = raw.split(";", 1)[0]
            if "=" not in first:
                continue
            name, value = first.split("=", 1)
            name = name.strip()
            if not name:
                continue
            low = raw.lower()
            expired = ("max-age=0" in low) or ("expires=thu, 01 jan 1970" in low)
            with self._lock:
                if expired:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value.strip()

    # ---------------------------------------------------------------- 请求主体
    def url_for(self, path):
        if not path:
            return self.base_url
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def request(
        self,
        path,
        method="GET",
        json_body=None,
        form_body=None,
        files=None,
        headers=None,
        tag=None,
        retries=None,
        allow_redirect=True,
    ):
        """发一次请求，返回 Response。网络错误会按 retries 退避重试。"""
        url = self.url_for(path)
        data = None
        extra = {}
        if files is not None:
            data, ctype = multipart_body(files)
            extra["Content-Type"] = ctype
        elif json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            # 与前端 fetch 的 Content-Type 保持一致，别加 charset 招惹严格的服务端
            extra["Content-Type"] = "application/json"
        elif form_body is not None:
            items = []
            if isinstance(form_body, dict):
                items = list(form_body.items())
            else:
                items = list(form_body)
            data = urllib.parse.urlencode(items, doseq=True).encode("utf-8")
            extra["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

        attempts = retries if retries is not None else self.retries
        last_error = None
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, method=method.upper())
            req.add_header("User-Agent", self.user_agent)
            req.add_header("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
            req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.6")
            if self.referer:
                req.add_header("Referer", self.referer)
            if method.upper() not in ("GET", "HEAD"):
                parts = urllib.parse.urlsplit(self.base_url)
                if parts.netloc:
                    req.add_header("Origin", "%s://%s" % (parts.scheme, parts.netloc))
            cookie = self.cookie_header()
            if cookie:
                req.add_header("Cookie", cookie)
            for key, value in extra.items():
                req.add_header(key, value)
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            opener = self._opener if allow_redirect else self._opener_nr
            try:
                try:
                    resp = opener.open(req, timeout=self.timeout)
                    status = resp.getcode()
                    resp_headers = resp.headers
                    body = resp.read()
                except urllib.error.HTTPError as err:
                    status = err.code
                    resp_headers = err.headers
                    body = err.read()
                self._absorb_cookies(resp_headers)
                result = Response(status, resp_headers, body, url)
                self._dump(result, method, json_body, form_body, tag)
                if status >= 500 and attempt < attempts:
                    last_error = ApiError(status, "服务端错误", result.text[:400], url)
                    time.sleep(min(2.0 * attempt, 5.0))
                    continue
                return result
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as err:
                last_error = NetworkError("连接失败：%s" % (err,))
                if attempt < attempts:
                    time.sleep(min(1.0 * attempt, 4.0))
                    continue
                raise last_error
        raise last_error or NetworkError("请求失败")

    # ------------------------------------------------------------------- 落盘
    def _dump(self, response, method, json_body, form_body, tag):
        if not self.save_raw:
            return
        try:
            if not os.path.isdir(self.raw_dir):
                os.makedirs(self.raw_dir)
        except Exception:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
        name = tag or urllib.parse.urlsplit(response.url).path.strip("/").replace("/", "-")
        if self.tag_prefix:
            name = "%s-%s" % (self.tag_prefix, name)
        name = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in name)[:60]
        body_text = response.text
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "url": response.url,
            "status": response.status,
            "request_json": json_body,
            "request_form": None if json_body is not None else form_body,
            "response_body": body_text[:300000],
            "response_truncated": len(body_text) > 300000,
            "content_type": (response.headers.get("Content-Type") if response.headers else ""),
        }
        path = os.path.join(self.raw_dir, "%04d-%s.json" % (seq, name))
        try:
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=1))
        except Exception:
            pass

    # ------------------------------------------------------------------- JSON
    def json_request(self, path, method="GET", body=None, tag=None, retries=None):
        """发请求并解析 JSON；401/被踢回登录页统一抛 Unauthorized。"""
        resp = self.request(
            path, method=method, json_body=body, tag=tag, retries=retries
        )
        data = resp.json()
        if resp.status == 401:
            raise Unauthorized(401, _extract_message(data) or "未登录或登录已过期", None, resp.url)
        if resp.status >= 400:
            raise ApiError(resp.status, _extract_message(data) or ("请求失败 %s" % resp.status), resp.text[:400], resp.url)
        if data is None:
            head = (resp.text or "").lstrip()[:1]
            if path.startswith("/") and "/api/" in path and head in ("<", ""):
                raise Unauthorized(401, "会话已失效（接口被重定向到登录页）", resp.text[:400], resp.url)
            raise ApiError(resp.status, "服务端返回的不是 JSON", resp.text[:400], resp.url)
        if resp.status in (301, 302, 303, 307, 308):
            raise ApiError(resp.status, "意外的重定向", None, resp.url)
        return data


def _extract_message(data):
    """从错误响应里挖出可读消息。"""
    if isinstance(data, dict):
        for key in ("detail", "message", "msg", "error", "errors"):
            value = data.get(key)
            if not value:
                continue
            if isinstance(value, str):
                return value
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
    if isinstance(data, str) and data.strip():
        return data.strip()[:300]
    return ""
