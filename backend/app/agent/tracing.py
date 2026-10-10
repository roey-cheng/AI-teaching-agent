"""LangSmith 执行级开关：必须包住整个 invoke/astream 消费过程，不只是创建 Agent。"""

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

# LangSmith 的实际接入位置；不把 settings 对象作为 trace 的输入。
from langsmith import Client, tracing_context

from app.core.config import TracingSettings
from app.core.async_work import complete_in_thread

logger = logging.getLogger(__name__)


@contextmanager
def agent_tracing(settings: TracingSettings) -> Iterator[None]:
    """终端/内部业务使用。启用后对话、模型提示和记忆可能上传到配置的 LangSmith 项目。

    显式 Client 确保 Pydantic 从 .env 读到的 key/project 生效，无需修改全局 os.environ。
    单元测试注入关闭配置或假的 Client；不会上传真实数据。
    """
    if not settings.tracing:
        with tracing_context(enabled=False):
            yield
        return

    api_key = settings.api_key
    if api_key is None:
        raise ValueError("LANGSMITH_API_KEY is required when tracing is enabled")
    client = Client(
        api_key=api_key.get_secret_value(), api_url=settings.endpoint,
        workspace_id=settings.workspace_id, timeout_ms=5000,
    )
    try:
        # LangGraph/模型已有 tracing 集成，不要给包含密钥的工厂参数额外加 @traceable。
        with tracing_context(enabled=True, client=client, project_name=settings.project):
            yield
    finally:
        # CLI 退出前给后台上传收尾；失败不覆盖原来的 Agent 异常，且不打印原始异常或密钥。
        try:
            client.close(timeout=5)
        except Exception:
            logger.warning("LangSmith tracing cleanup failed. Check tracing configuration and connectivity.")


@asynccontextmanager
async def async_agent_tracing(settings: TracingSettings) -> AsyncIterator[None]:
    """正式异步执行入口：ContextVar 在事件循环内设置，Client 收尾在线程里完成。"""
    if not settings.tracing:
        with tracing_context(enabled=False):
            yield
        return
    if settings.api_key is None:
        raise ValueError("LANGSMITH_API_KEY is required when tracing is enabled")
    client = Client(api_key=settings.api_key.get_secret_value(), api_url=settings.endpoint,
                    workspace_id=settings.workspace_id, timeout_ms=5000)
    try:
        with tracing_context(enabled=True, client=client, project_name=settings.project):
            yield
    finally:
        def close():
            try:
                client.close(timeout=5)
            except Exception:
                logger.warning("LangSmith tracing cleanup failed. Check tracing configuration and connectivity.")
        await complete_in_thread(close)
