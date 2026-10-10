"""SSE 传输层：只搬运白名单事件，不生成答案、不决定数据库成功状态。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from starlette.responses import JSONResponse, Response

from app.core.async_work import complete_before_cancelling
from app.schemas import (
    AgentProgressData, DuplicateMessageResponse, MessageDeltaData, MessageDoneData,
    MessageErrorData, MessageStartData, ReasoningDeltaData,
)
from app.services.chat_execution import ChatEvent

logger = logging.getLogger(__name__)
EVENT_TYPES = (MessageStartData, AgentProgressData, ReasoningDeltaData,
               MessageDeltaData, MessageDoneData, MessageErrorData)


def encode_event(event: ChatEvent, request_id: str) -> bytes:
    if type(event) not in EVENT_TYPES:
        raise ValueError("Unsupported public stream event")
    if isinstance(event, MessageErrorData):
        event = event.model_copy(update={"error": event.error.model_copy(update={"request_id": request_id})})
    # JSON 编码会转义正文中的换行，用户文字不能伪造另一个 event: 行。
    return f"event: {event.event_name}\ndata: {event.model_dump_json()}\n\n".encode("utf-8")


@dataclass
class _Finished:
    result: object = None
    error: Exception | None = None


class ChatStreamResponse(Response):
    """在响应作用域内运行执行器，连同断线监听一起管理，不能遗留后台模型任务。

    等到 message_start 才发送 SSE 响应头；准入失败仍能返回 4xx/5xx JSON，
    重复请求仍是 200 JSON。队列有界，慢浏览器不能无限堆积模型输出。
    """

    media_type = "text/event-stream"

    def __init__(self, execute: Callable, *, request_id: str, heartbeat_seconds=15.0,
                 send_timeout_seconds=15.0, queue_size=8, active_requests=None):
        super().__init__(status_code=200)
        self.execute = execute
        self.request_id = request_id
        self.heartbeat_seconds = heartbeat_seconds
        self.send_timeout_seconds = send_timeout_seconds
        self.queue_size = queue_size
        self.active_requests = active_requests

    async def __call__(self, scope, receive, send):
        task = asyncio.current_task()
        if self.active_requests is not None:
            self.active_requests.add(task)
        try:
            await self._run(scope, receive, send)
        finally:
            if self.active_requests is not None:
                self.active_requests.discard(task)

    async def _run(self, scope, receive, send):
        queue = asyncio.Queue(maxsize=self.queue_size)
        closing = False

        async def emit(event):
            # 只存已编码公开事件；不把整个 Agent state 放进队列。
            frame = encode_event(event, self.request_id)
            await queue.put((event, frame))

        async def produce():
            try:
                result = await self.execute(emit)
            except asyncio.CancelledError:
                if not closing:
                    # 供应商自行取消不能让消费者永久等待；传输主动取消时不再入队。
                    await queue.put(_Finished(error=RuntimeError("Execution was unexpectedly cancelled")))
                raise
            except Exception as error:
                await queue.put(_Finished(error=error))
            else:
                await queue.put(_Finished(result=result))

        async def write(message):
            # ASGI send 挂起也有上限；触发后取消执行器，等待其安全清理。
            await asyncio.wait_for(send(message), timeout=self.send_timeout_seconds)

        async def consume():
            started = False
            attempt_id = None
            terminal = False
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=self.heartbeat_seconds)
                except TimeoutError:
                    if started:
                        await write({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
                    continue
                if isinstance(item, _Finished):
                    if not started:
                        if item.error is not None:
                            raise item.error  # 交给统一 HTTP 错误处理器；还没发响应头。
                        if not isinstance(item.result, DuplicateMessageResponse):
                            raise RuntimeError("Execution ended without a public start event")
                        response = JSONResponse(item.result.model_dump(mode="json"))
                        await response(scope, receive, write)
                    else:
                        if item.error is not None or not terminal:
                            # 结算无法确认时，不编造 message_error/FAILED。前端须重查历史。
                            logger.error("SSE ended without confirmed terminal; request_id=%s", self.request_id)
                        await write({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                event, frame = item
                if not started:
                    if not isinstance(event, MessageStartData):
                        raise RuntimeError("Execution did not start with message_start")
                    attempt_id = event.attempt_id
                    await write({"type": "http.response.start", "status": 200, "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-cache, no-store"),
                        (b"x-accel-buffering", b"no"),
                    ]})
                    started = True
                elif isinstance(event, MessageStartData):
                    raise RuntimeError("Duplicate stream start")
                if terminal or event.attempt_id != attempt_id:
                    raise RuntimeError("Inconsistent stream execution identity")
                terminal = isinstance(event, (MessageDoneData, MessageErrorData))
                await write({"type": "http.response.body", "body": frame, "more_body": True})

        async def disconnected():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    return

        producer = asyncio.create_task(produce())
        consumer = asyncio.create_task(consume())
        watcher = asyncio.create_task(disconnected())
        tasks = (producer, consumer, watcher)
        try:
            done, _ = await asyncio.wait((consumer, watcher), return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:
                consumer.result()
            else:
                watcher.result()
        except (OSError, TimeoutError):
            # 已断线/无法继续发送；不能对坏连接再报一次 HTTP 错误。
            pass
        finally:
            closing = True
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 等待执行器关闭模型、确认在途提交及释放占用；重复 cancel 不能提前放行。
            await complete_before_cancelling(asyncio.gather(*tasks, return_exceptions=True))
