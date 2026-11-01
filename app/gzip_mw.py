"""Compression of the pages and data the app sends (scripts, styles, JSON): the main script alone was 239 KB, about 76 KB compressed, and a slow
connection spends its whole waiting time on those bytes.

The live price stream (/api/stream, Server-Sent Events) is deliberately NOT compressed: compressing a stream buffers it, and a price that waits in a
buffer is not live. Everything else goes through Starlette's GZip (answers under 1 KB are left alone, a client that does not ask for gzip gets plain bytes).
If nginx compresses as well (deploy/nginx-gzip.conf) nothing is compressed twice: nginx leaves an answer that is already encoded as it is."""
from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware

NEVER_COMPRESSED = ("/api/stream",)


class SelectiveGZip:
    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 5) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(NEVER_COMPRESSED):
            return await self.app(scope, receive, send)
        return await self.gzip(scope, receive, send)
