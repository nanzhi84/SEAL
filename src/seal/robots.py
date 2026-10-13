"""Robots policy is fetched through the same bounded transport, before content."""

import asyncio
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .core import SealError


class Robots:
    def __init__(self, config):
        self.config = config
        self.cache = {}
        self.lock = asyncio.Lock()

    async def check(self, url, fetch):
        if not self.config["robots"]:
            return
        p = urlsplit(url)
        origin = urlunsplit((p.scheme, p.netloc, "", "", ""))
        async with self.lock:
            if origin not in self.cache:
                status, body = await fetch(origin + "/robots.txt")
                if status in (404, 410):
                    policy = RobotFileParser()
                    policy.parse([])
                elif status == 200:
                    policy = RobotFileParser()
                    policy.parse(body.decode("utf-8", errors="replace").splitlines())
                else:
                    raise SealError("robots_unavailable")
                self.cache[origin] = policy
        if not self.cache[origin].can_fetch(self.config["user_agent"], url):
            raise SealError("robots_denied")
