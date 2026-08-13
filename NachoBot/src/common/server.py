import ipaddress
import inspect
import secrets
import warnings
from collections.abc import Iterable

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware  # 新增导入
from fastapi.responses import JSONResponse
from typing import Optional
from uvicorn import Config, Server as UvicornServer
import os
from rich.traceback import install

install(extra_lines=3)


def is_loopback_host(host: str) -> bool:
    """返回监听地址是否明确限定在本机。"""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _extract_auth_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return value.strip()
    # ncnk_message 历史上使用裸令牌，HTTP 在过渡期保持兼容。
    return authorization.strip()


def supports_message_server_token_auth(server_class: type) -> bool:
    """Check the imported implementation, not unrelated distribution metadata."""
    try:
        parameters = inspect.signature(server_class).parameters
    except (TypeError, ValueError):
        return False
    return "enable_token" in parameters and callable(
        getattr(server_class, "add_valid_token", None)
    )


class Server:
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, app_name: str = "NachoCore"):
        self.app = FastAPI(title=app_name)
        self._host: str = "127.0.0.1"
        self._port: int = 8080
        self._server: Optional[UvicornServer] = None
        self._auth_tokens: tuple[str, ...] = ()
        self.set_address(host, port)

        # 配置 CORS
        origins = [
            "http://localhost:3000",  # 允许的前端源
            "http://127.0.0.1:3000",
            # 在生产环境中，您应该添加实际的前端域名
        ]

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,  # 是否支持 cookie
            allow_methods=["*"],  # 允许所有 HTTP 方法
            allow_headers=["*"],  # 允许所有 HTTP 请求头
        )

        @self.app.get("/health", include_in_schema=False)
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @self.app.middleware("http")
        async def authenticate_api(request: Request, call_next):
            if (
                self._auth_tokens
                and request.method != "OPTIONS"
                and request.url.path.startswith("/api")
            ):
                supplied_token = _extract_auth_token(
                    request.headers.get("authorization")
                )
                if not any(
                    secrets.compare_digest(supplied_token, expected)
                    for expected in self._auth_tokens
                ):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "未授权"},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return await call_next(request)

    def configure_auth(self, tokens: Iterable[str]) -> None:
        """配置 Core HTTP API 和消息服务共用的令牌集合。"""
        environment_token = os.getenv("NACHOBOT_CORE_TOKEN", "").strip()
        self._auth_tokens = tuple(
            dict.fromkeys(
                [
                    *(token.strip() for token in tokens if token and token.strip()),
                    *([environment_token] if environment_token else []),
                ]
            )
        )

    @property
    def auth_tokens(self) -> tuple[str, ...]:
        """Return the normalized credentials shared by HTTP and WebSocket."""
        return self._auth_tokens

    def validate_security(self) -> None:
        """防止在无认证时将 Core 控制面监听到非回环地址。"""
        if not is_loopback_host(self._host) and not self._auth_tokens:
            trusted_container_network = os.getenv(
                "NACHOBOT_TRUSTED_CONTAINER_NETWORK", ""
            ).strip().lower() in {"1", "true", "yes"}
            if trusted_container_network:
                warnings.warn(
                    "Core 以无令牌模式监听容器网络；请确保宿主机端口仅绑定回环地址。",
                    RuntimeWarning,
                    stacklevel=2,
                )
                return
            raise RuntimeError(
                "Core 拒绝在无认证时监听非回环地址。"
                "请设置 NACHOBOT_CORE_TOKEN 或在 [ncnk_message].auth_token 中配置令牌，"
                "或将 HOST 设为 127.0.0.1。只有受信任的容器网络才可显式设置 "
                "NACHOBOT_TRUSTED_CONTAINER_NETWORK=1。"
            )

    def register_router(self, router: APIRouter, prefix: str = ""):
        """注册路由

        APIRouter 用于对相关的路由端点进行分组和模块化管理：
        1. 可以将相关的端点组织在一起，便于管理
        2. 支持添加统一的路由前缀
        3. 可以为一组路由添加共同的依赖项、标签等

        示例:
            router = APIRouter()

            @router.get("/users")
            def get_users():
                return {"users": [...]}

            @router.post("/users")
            def create_user():
                return {"msg": "user created"}

            # 注册路由，添加前缀 "/api/v1"
            server.register_router(router, prefix="/api/v1")
        """
        self.app.include_router(router, prefix=prefix)

    def set_address(self, host: Optional[str] = None, port: Optional[int] = None):
        """设置服务器地址和端口"""
        if host:
            self._host = host
        if port:
            self._port = port

    async def run(self):
        """启动服务器"""
        self.validate_security()
        # 禁用 uvicorn 默认日志和访问日志
        config = Config(app=self.app, host=self._host, port=self._port, log_config=None, access_log=False)
        self._server = UvicornServer(config=config)
        try:
            await self._server.serve()
        except KeyboardInterrupt:
            await self.shutdown()
            raise
        except Exception as e:
            await self.shutdown()
            raise RuntimeError(f"服务器运行错误: {str(e)}") from e
        finally:
            await self.shutdown()

    async def shutdown(self):
        """安全关闭服务器"""
        if self._server:
            self._server.should_exit = True
            try:
                if hasattr(self._server, "servers"):
                    await self._server.shutdown()
            except Exception:
                pass
            self._server = None

    def get_app(self) -> FastAPI:
        """获取 FastAPI 实例"""
        return self.app


global_server = None


def get_global_server() -> Server:
    """获取全局服务器实例"""
    global global_server
    if global_server is None:
        global_server = Server(host=os.environ["HOST"], port=int(os.environ["PORT"]))
    return global_server
