"""Recognise social-media / platform URLs and extract the account handle.

Social platforms (Instagram, X, Facebook, LinkedIn, TikTok ...) usually hide profile
content behind a login wall for anonymous HTTP clients. Knowing that a URL is a
social profile lets the service (a) analyse the *account owner* rather than the
platform itself and (b) fall back to public web research when the page is empty.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

PLATFORM_DOMAINS: dict[str, str] = {
    "instagram.com": "Instagram",
    "x.com": "X",
    "twitter.com": "X",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "threads.net": "Threads",
    "threads.com": "Threads",
    "pinterest.com": "Pinterest",
    "reddit.com": "Reddit",
}

# Path segments that are platform pages, not account handles.
_NON_HANDLES = {
    "", "p", "reel", "reels", "stories", "explore", "watch", "shorts", "status", "home", "search",
    "hashtag", "i", "intent", "share", "sharer", "groups", "events", "pages", "pg", "profile.php",
    "company", "in", "school", "showcase", "channel", "c", "user", "r", "u", "pin", "video",
}


@dataclass(frozen=True)
class PlatformInfo:
    name: str               # e.g. "Instagram"
    handle: str | None      # e.g. "crestspot"
    profile_kind: str       # "profile" | "post" | "page"

    @property
    def label(self) -> str:
        return f"{self.name} {self.profile_kind}" + (f" (@{self.handle})" if self.handle else "")


def detect_platform(url: str) -> PlatformInfo | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile.", "web.", "business."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    name = next((n for d, n in PLATFORM_DOMAINS.items() if host == d or host.endswith("." + d)), None)
    if not name:
        return None

    segs = [s for s in parts.path.split("/") if s]
    lower = [s.lower() for s in segs]
    handle: str | None = None
    kind = "profile"

    if name == "LinkedIn" and len(segs) >= 2 and lower[0] in ("company", "in", "school", "showcase"):
        handle = segs[1]
        kind = "page" if lower[0] != "in" else "profile"
    elif name == "YouTube":
        if segs and segs[0].startswith("@"):
            handle = segs[0][1:]
        elif len(segs) >= 2 and lower[0] in ("channel", "c", "user"):
            handle = segs[1]
        elif lower[:1] in (["watch"], ["shorts"]) or host == "youtu.be":
            kind = "post"
    elif name == "Reddit" and len(segs) >= 2 and lower[0] in ("r", "user", "u"):
        handle = segs[1]
        kind = "page" if lower[0] == "r" else "profile"
    elif name == "Facebook" and lower[:1] == ["profile.php"]:
        handle = None
    elif segs and lower[0] not in _NON_HANDLES:
        handle = segs[0].lstrip("@")
        if len(lower) > 1 and lower[1] in ("status", "p", "reel", "video", "posts"):
            kind = "post"
    elif lower[:1] and lower[0] in ("p", "reel", "reels", "status", "pin", "video"):
        kind = "post"

    return PlatformInfo(name=name, handle=handle or None, profile_kind=kind)
