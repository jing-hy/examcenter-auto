# -*- coding: utf-8 -*-
"""隐私工具：账号等个人信息只在内存与控制台出现，绝不落到磁盘。

规则：
* 文件名里的账号段一律换成不可逆的 hash id（acc-3f2a91c4），
  这样 logs/status 目录不会出现"谁是哪个学号"；
* 写日志文件之前，把已知敏感串（账号、姓名、密码、Cookie）替换成 ***；
* 控制台仍然显示完整账号，方便你自己看，但那条输出不落盘。

只依赖标准库，Python 3.7 可跑。
"""

import hashlib
import re

MASK = "***"

# 太短的值（1 个字符）不做替换，否则会把正文里的单字全换掉
MIN_SECRET_LEN = 2


def hash_id(text, length=8):
    """稳定、不可逆的短 id。"""
    raw = str(text or "").encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:max(4, int(length or 8))]


def slug(text):
    """文件名安全的匿名 id：acc-3f2a91c4。"""
    return "acc-" + hash_id(text)


def mask_account(text):
    """控制台/日志里可以展示的遮罩账号，保留极少信息以便区分。"""
    raw = str(text or "").strip()
    if not raw:
        return MASK
    if len(raw) <= 2:
        return raw[0] + MASK
    return raw[:2] + MASK + "(" + hash_id(raw, 4) + ")"


def clean_secrets(values):
    """过滤掉空串与过短的串。"""
    items = []
    for value in values or []:
        text = str(value or "").strip()
        if len(text) >= MIN_SECRET_LEN:
            items.append(text)
    # 长的先替换，避免短串先动手把长串拆坏
    items.sort(key=len, reverse=True)
    return items


def redact_text(text, secrets):
    """把 text 里出现的敏感串替换成 ***。"""
    result = text if isinstance(text, str) else str(text)
    for secret in secrets or []:
        if secret:
            result = result.replace(secret, MASK)
    return result


def redact_url(url, secrets):
    """日志里出现的 URL 也顺手脱敏（有些系统把账号塞进 query）。"""
    return redact_text(url, secrets)
