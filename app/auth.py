"""访问控制：可选的访问令牌 + 跨站请求拦截。

考虑的问题：

1. 这个服务能读整个媒体库、能删源文件，但默认没有任何鉴权。
   只要端口被映射出去（尤其 docker 默认发布到 0.0.0.0），同网段任何人打开就是管理员。
   所以加了 ACCESS_TOKEN：设了就必须带令牌，不设维持原来的「信任环境」行为。

2. 即便不设令牌，浏览器里的任何页面也能跨站 POST 过来（简单请求绕过 CORS 预检）。
   /api/jobs/clear 这种空 body 的接口以前一个 <form> 就能打。
   这里对写操作加了三条硬性检查：Sec-Fetch-Site 不能是 cross-site、
   Origin 必须与 Host 同源、带 body 的请求必须是 application/json。
   浏览器会自动带上前两个头，命令行工具不带，所以不影响 curl 脚本。

3. 令牌用 Cookie 承载时，SameSite=Strict + HttpOnly 能挡住跨站携带，
   同时页面里的 JS 读不到令牌本身。
"""
from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config

log = logging.getLogger("media-shrink.auth")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# 不需要令牌的路径：容器健康检查 + 站点图标 + 静态资源。
# 静态资源只是前端代码，不含媒体库信息；放行后登录页也能正常显示样式。
PUBLIC_EXACT = (
    "/api/health",
    "/favicon.ico",
    "/favicon.svg",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
)
PUBLIC_PREFIX = ("/static/",)

_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>MediaShrink · 需要访问令牌</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#0f1115; color:#e6e8ee; }}
  .card {{ width:min(92vw,380px); padding:28px; border-radius:16px;
          background:#181b22; border:1px solid #262a34; box-shadow:0 20px 60px rgba(0,0,0,.45); }}
  h1 {{ margin:0 0 6px; font-size:18px; }}
  p  {{ margin:0 0 16px; color:#98a0b3; font-size:13px; }}
  input {{ width:100%; box-sizing:border-box; padding:10px 12px; border-radius:10px;
          border:1px solid #333846; background:#11141a; color:inherit; font-size:14px; }}
  input:focus {{ outline:2px solid #4c8dff; outline-offset:1px; }}
  button {{ width:100%; margin-top:12px; padding:10px 12px; border:0; border-radius:10px;
           background:#2f6df6; color:#fff; font-size:14px; font-weight:600; cursor:pointer; }}
  .err {{ margin:0 0 12px; padding:8px 10px; border-radius:8px;
         background:#3a1d20; color:#ff9ea6; font-size:12px; }}
  code {{ background:#11141a; padding:1px 5px; border-radius:5px; font-size:12px; }}
</style>
</head>
<body>
<form class="card" method="get" action="/">
  <h1>MediaShrink</h1>
  <p>这个实例启用了访问令牌，请输入 <code>ACCESS_TOKEN</code> 后继续。</p>
  {error}
  <input name="token" type="password" placeholder="访问令牌" autofocus autocomplete="current-password">
  <button type="submit">进入</button>
</form>
</body>
</html>
"""

_ERROR_HTML = '<p class="err">令牌不正确，请重试。</p>'


def _match(token: str) -> bool:
    """常量时间比较，避免通过响应时间猜令牌。"""
    expected = config.ACCESS_TOKEN
    if not expected or not token:
        return False
    return secrets.compare_digest(token.encode("utf-8", "ignore"), expected.encode("utf-8", "ignore"))


def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIX)


def _has_token(request: Request) -> bool:
    header = request.headers.get("x-auth-token")
    if header and _match(header.strip()):
        return True
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and _match(auth[7:].strip()):
        return True
    cookie = request.cookies.get(config.TOKEN_COOKIE_NAME)
    return bool(cookie and _match(cookie))


def _state_reason(request: Request) -> tuple[int, str]:
    """写操作的同源校验，返回 (状态码, 拒绝原因)；通过则 (0, "")。"""
    if request.method in SAFE_METHODS:
        return 0, ""

    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site not in ("same-origin", "same-site", "none"):
        return 403, "拒绝跨站请求"

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        host = (request.headers.get("host") or "").strip().lower()
        netloc = urlsplit(origin).netloc.lower()
        if host and netloc and netloc != host:
            return 403, "请求来源与本站不一致"

    # 表单（application/x-www-form-urlencoded、text/plain、multipart）属于「简单请求」，
    # 不需要预检就能发出，是最常见的 CSRF 载体。接口只收 JSON。
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith("application/json"):
        return 415, "POST 只接受 application/json"
    return 0, ""


def _target_without_token(request: Request) -> str:
    """把 ?token=xxx 摘掉，其余查询参数原样保留（比如 ?scan=/media/movies）。"""
    rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
    return "/" + (f"?{urlencode(rest)}" if rest else "")


async def dispatch(request: Request, call_next):
    status, reason = _state_reason(request)
    if status:
        return JSONResponse({"detail": reason}, status_code=status)

    if config.ACCESS_TOKEN and not _is_public(request.url.path):
        path = request.url.path
        if path in ("/", "/index.html") and "token" in request.query_params:
            if _match(request.query_params.get("token") or ""):
                resp = RedirectResponse(_target_without_token(request), status_code=303)
                resp.set_cookie(
                    config.TOKEN_COOKIE_NAME,
                    config.ACCESS_TOKEN,
                    max_age=config.TOKEN_COOKIE_DAYS * 86400,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
                return resp
            return HTMLResponse(_LOGIN_HTML.format(error=_ERROR_HTML), status_code=401)

        if not _has_token(request):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "需要访问令牌（请求头 X-Auth-Token）"}, status_code=401)
            return HTMLResponse(_LOGIN_HTML.format(error=""), status_code=401)

    return await call_next(request)


def install(app: FastAPI) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    # 注意注册顺序：越晚注册越靠外层。这里在其它中间件之前注册，
    # 保证后注册的「防缓存」中间件仍能给 401/403 响应加上 no-store。
    app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)


def warn_if_open() -> None:
    """启动时把风险说清楚，别让「以为内网很安全」变成事故。"""
    if config.ACCESS_TOKEN:
        log.info("访问令牌已启用（ACCESS_TOKEN 已设置）")
        return
    log.warning(
        "未设置 ACCESS_TOKEN：任何能访问本端口的人都可以扫描媒体库、%s。"
        "如果端口暴露到内网以外，请设置 ACCESS_TOKEN 后重启。",
        "删除源文件" if config.ALLOW_SOURCE_DELETE else "提交转码任务",
    )
