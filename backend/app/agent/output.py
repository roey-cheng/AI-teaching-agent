"""只读取主模型对外返回的文字；不把工具参数、记忆或 LangGraph 状态当成回复。"""

from dataclasses import dataclass
from contextlib import asynccontextmanager

from langchain_core.messages import AIMessageChunk

from app.agent.budget_middleware import AgentToolUseNotAllowedError
from app.core.async_work import complete_before_cancelling


@asynccontextmanager
async def managed_agent_stream(stream):
    """消费可取消；关闭过程等完再传播重复取消，避免清理半途就释放运行占用。"""
    try:
        yield stream
    finally:
        await complete_before_cancelling(stream.aclose())


@dataclass(frozen=True)
class VisibleChunk:
    reasoning: str = ""
    answer: str = ""
    finish_reason: str | None = None


def extract_visible_chunk(chunk, metadata: dict) -> VisibleChunk:
    if not isinstance(chunk, AIMessageChunk) or metadata.get("langgraph_node") != "model":
        return VisibleChunk()
    if (chunk.tool_calls or chunk.invalid_tool_calls or chunk.tool_call_chunks
            or chunk.additional_kwargs.get("tool_calls")):
        raise AgentToolUseNotAllowedError()
    reasoning = chunk.additional_kwargs.get("reasoning_content", "") or ""
    if not isinstance(reasoning, str):
        raise ValueError("Invalid model reasoning payload")
    answer = ""
    block_reasoning = ""
    if isinstance(chunk.content, str):
        answer = chunk.content
    else:
        for block in chunk.content:
            if isinstance(block, str):
                answer += block
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    answer += block["text"]
                elif block.get("type") == "reasoning" and isinstance(block.get("reasoning"), str):
                    block_reasoning += block["reasoning"]
    # 有些适配器同时保留供应商字段和标准块，只取一份，避免重复展示。
    return VisibleChunk(reasoning or block_reasoning, answer, chunk.response_metadata.get("finish_reason"))
