"""临时 HTTPS 演示入口：同一个单进程提供 API 和打包后的前端。"""

import argparse
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.http_config import HTTPSettings
from app.main import create_app

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_demo_app(public_origin: str, *, runtime_factory=None, dist: Path = FRONTEND_DIST):
    settings = HTTPSettings.from_sources(
        _env_file=None,
        allowed_origins=[public_origin],
        cookie_secure=True,
    )
    parsed = urlsplit(public_origin)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError("The public demo origin must use HTTPS.")
    if not (dist / "index.html").is_file():
        raise ValueError("Build the frontend first: cd frontend && npm run build")
    application = create_app(runtime_factory=runtime_factory, http_settings=settings)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[parsed.hostname, "127.0.0.1", "localhost"],
        www_redirect=False,
    )
    # 只公开 dist；不能把项目根目录或 backend（含 .env）作为静态目录。
    # API 路由先注册，静态页面最后匹配，不经过 Vite 开发服务器。
    application.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return application


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("public_origin", help="Exact HTTPS tunnel origin, without a trailing slash")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    application = create_demo_app(args.public_origin)
    # 隧道连接到回环地址。全程单进程，保留项目已有的执行互斥和启动清理。
    # 不信任外部传来的客户端 IP；本次小规模演示共享 IP 级限流。
    uvicorn.run(application, host="127.0.0.1", port=args.port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
