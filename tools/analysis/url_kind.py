"""URL 平台判定：按 hostname 精确匹配，不靠子串包含。

子串判断（`"youtube.com" in url`）会把 `https://evil.com/?u=youtube.com`
或 `https://youtube.com.evil.com/...` 误判成目标平台，进而走错下载/解析分支。
这里统一解析 hostname，再按「等于域名或以 `.域名` 结尾」匹配。
"""
from __future__ import annotations

from urllib.parse import urlparse


def host_of(url: str) -> str:
    """取小写 hostname；无法解析时返回空串。"""
    candidate = url if "://" in url else f"https://{url}"
    try:
        return (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return ""


def path_of(url: str) -> str:
    candidate = url if "://" in url else f"https://{url}"
    try:
        return (urlparse(candidate).path or "").lower()
    except ValueError:
        return ""


def host_matches(host: str, domain: str) -> bool:
    """host 等于该域名，或是它的子域；`youtube.com.evil.com` 不算。"""
    host = (host or "").lower().lstrip(".")
    return host == domain or host.endswith(f".{domain}")


def platform_of(url: str) -> str:
    """返回 shorts / youtube / instagram / tiktok / vimeo / twitter / other_url。"""
    host = host_of(url)
    path = path_of(url)
    if host_matches(host, "youtube.com") or host == "youtu.be":
        return "shorts" if "/shorts" in path else "youtube"
    for domain, name in (
        ("instagram.com", "instagram"),
        ("tiktok.com", "tiktok"),
        ("vimeo.com", "vimeo"),
    ):
        if host_matches(host, domain):
            return name
    if host_matches(host, "twitter.com") or host_matches(host, "x.com"):
        return "twitter"
    return "other_url"
