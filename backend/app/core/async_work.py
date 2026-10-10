"""让短同步操作离开事件循环；取消不能把仍在提交的数据库线程丢在后台。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def complete_in_thread(function: Callable[[], T]) -> T:
    """等待线程确定结果后再传播取消。不能用来运行模型或长时间任务。

    asyncio.to_thread 本身不会停止底层线程。shield + 等待保证调用方开始清理时，
    前一笔数据库事务已经结束；重复 cancel 也不能提前释放会话。
    """
    return await complete_before_cancelling(asyncio.to_thread(function))


async def complete_before_cancelling(work: Awaitable[T]) -> T:
    """只用于短事务/资源收尾，不能包整个模型调用，否则模型超时将无法取消。"""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # 取出异常，避免未读取的后台异常；取消优先，由上层重新核对数据库。
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()
