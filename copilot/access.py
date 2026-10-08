"""Single-workspace access gate for a private cloud demonstration."""
import base64
import binascii
import hmac
import os
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def validate_access_config():
    password = os.getenv("QA_ACCESS_PASSWORD", "")
    if os.getenv("QA_DEPLOYMENT", "local") == "public" and len(password) < 16:
        raise RuntimeError("Public deployment requires QA_ACCESS_PASSWORD with at least 16 characters")


class AccessGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        password = os.getenv("QA_ACCESS_PASSWORD", "")
        public = os.getenv("QA_DEPLOYMENT", "local") == "public"
        if public and len(password) < 16:
            return await JSONResponse({"detail": "Access protection is not configured"}, status_code=503)(scope, receive, send)
        if not password:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        authorized = False
        try:
            scheme, token = headers.get(b"authorization", b"").decode("ascii").split(" ", 1)
            username, supplied = base64.b64decode(token, validate=True).decode("utf-8").split(":", 1)
            authorized = (scheme.lower() == "basic"
                          and hmac.compare_digest(username.encode(), os.getenv("QA_ACCESS_USER", "admin").encode())
                          and hmac.compare_digest(supplied.encode(), password.encode()))
        except (ValueError, UnicodeError, binascii.Error):
            pass
        if not authorized:
            return await JSONResponse({"detail": "请输入访问账号和密码"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Qalio", charset="UTF-8"', "Cache-Control": "no-store"})(scope, receive, send)
        # Browsers may attach Basic credentials automatically; reject cross-site writes.
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            origin = headers.get(b"origin", b"").decode("latin-1")
            host = headers.get(b"host", b"").decode("latin-1")
            if headers.get(b"sec-fetch-site") == b"cross-site" or (origin and (urlsplit(origin).netloc != host or urlsplit(origin).scheme != scope["scheme"])):
                return await JSONResponse({"detail": "Cross-site writes are not allowed"}, status_code=403)(scope, receive, send)

        async def protected_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message["headers"]) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin")]
            await send(message)
        await self.app(scope, receive, protected_send)
