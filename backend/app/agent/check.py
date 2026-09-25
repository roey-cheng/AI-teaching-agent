"""运行：uv run python -m app.agent.check（会产生一次付费模型调用）。"""

import asyncio
from contextlib import aclosing

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_model_call
from langchain_core.messages import AIMessageChunk
from langchain_deepseek import ChatDeepSeek
from langsmith import tracing_context
from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.core.config import ModelSettings, load_model_settings

QUESTION = "What is an API? Explain in two sentences in English. Do not use tools."


@wrap_model_call
async def probe_without_tools(request, handler):
    # create_deep_agent 自带工具，tools=[] 并不能移除它们。
    # 此次只测试回答，因此在交给模型前隐藏所有工具，避免额外动作和费用。
    return await handler(request.override(tools=[]))


def build_probe_agent(settings: ModelSettings):
    # ChatDeepSeek 是连接 DeepSeek 的适配器；此处创建对象还没有发问题。
    model = ChatDeepSeek(
        model=settings.name,
        api_key=settings.api_key,
        api_base=settings.base_url,
        max_tokens=128,
        timeout=30,
        max_retries=0,
        # 最小测试不用思考模式，将有限输出预算留给用户可见回答。
        extra_body={"thinking": {"type": "disabled"}},
    )
    # 真正使用 Deep Agents 构建执行流程，而不是直接 model.invoke()。
    # 默认工作区只在内存里，不挂载项目文件，也不接数据库或长期记忆。
    return create_deep_agent(
        model=model,
        system_prompt="你是教学助手。直接简短回答用户问题，不要调用工具。",
        middleware=[probe_without_tools, ModelCallLimitMiddleware(run_limit=1)],
        checkpointer=None,
    )


def visible_text(chunk, metadata) -> str:
    # 只展示主模型的文字，不显示工具参数、思考过程或其他内部消息。
    if not isinstance(chunk, AIMessageChunk) or metadata.get("langgraph_node") != "model":
        return ""
    if chunk.tool_call_chunks:
        return ""
    if isinstance(chunk.content, str):
        return chunk.content
    return "".join(
        block.get("text", "")
        for block in chunk.content
        if isinstance(block, dict) and block.get("type") == "text"
    )


async def stream_answer(agent) -> int:
    count = 0
    finish_reason = None
    # astream 像不断收到小纸条；每来一段文字就打印一段，不等全部生成完。
    async with aclosing(agent.astream(
        {"messages": [{"role": "user", "content": QUESTION}]},
        config={"recursion_limit": 8},
        stream_mode="messages",
        subgraphs=False,
    )) as stream:
        async for chunk, metadata in stream:
            if isinstance(chunk, AIMessageChunk) and metadata.get("langgraph_node") == "model":
                finish_reason = chunk.response_metadata.get("finish_reason", finish_reason)
            text = visible_text(chunk, metadata)
            if text:
                print(text, end="", flush=True)
                count += 1
    if not count or finish_reason != "stop":
        raise RuntimeError("No complete answer with a normal finish was received")
    return count


async def main() -> int:
    try:
        settings = load_model_settings()
    except (ValidationError, SettingsError, OSError):
        print("Invalid model configuration: check MODEL_* in backend/.env; the API key must have no whitespace and the URL must be an official DeepSeek endpoint.")
        return 1

    print("Starting the minimal Deep Agents check (one model call; no database reads or writes).")
    print(f"Question: {QUESTION}\nAnswer: ", end="", flush=True)
    try:
        # 不向 LangSmith 上传这次运行记录。总等待不超过 60 秒。
        with tracing_context(enabled=False):
            async with asyncio.timeout(60):
                count = await stream_answer(build_probe_agent(settings))
    except AuthenticationError:
        print("\nAuthentication failed: check your API key; do not share it in chat.")
        return 1
    except (APITimeoutError, TimeoutError):
        print("\nRequest timed out: the check did not complete; no automatic retry.")
        return 1
    except APIConnectionError:
        print("\nNetwork connection failed: check your network and proxy; no automatic retry.")
        return 1
    except APIStatusError as error:
        print(f"\nModel service returned HTTP {error.status_code}: check the model name, account balance, or rate limits.")
        return 1
    except Exception:
        # 不打印原始异常、请求对象或设置，避免意外泄露密钥。
        print("\nCheck incomplete: model output was incomplete or the Agent failed; no automatic retry.")
        return 1
    print(f"\nCheck successful: received {count} non-empty text chunks; the model finished normally.")
    if count < 2:
        print("Only one text chunk was received; multiple incremental chunks have not yet been observed.")
    print("This is a terminal-only test; web chat, SSE endpoints, and long-term memory are not integrated yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
