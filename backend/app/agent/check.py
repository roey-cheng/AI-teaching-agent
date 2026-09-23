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
        raise RuntimeError("没有收到正常结束的完整回答")
    return count


async def main() -> int:
    try:
        settings = load_model_settings()
    except (ValidationError, SettingsError, OSError):
        print("模型配置无效：检查 backend/.env 的 MODEL_*，密钥不能有空格，地址须为 DeepSeek 官方地址。")
        return 1

    print("开始 Deep Agents 最小检查（一次模型调用；不读写数据库）。")
    print(f"问题：{QUESTION}\n回答：", end="", flush=True)
    try:
        # 不向 LangSmith 上传这次运行记录。总等待不超过 60 秒。
        with tracing_context(enabled=False):
            async with asyncio.timeout(60):
                count = await stream_answer(build_probe_agent(settings))
    except AuthenticationError:
        print("\n认证失败：请检查 API Key；不要把密钥发到聊天里。")
        return 1
    except (APITimeoutError, TimeoutError):
        print("\n请求超时：本次未完成，不自动重试。")
        return 1
    except APIConnectionError:
        print("\n网络连接失败：检查网络及代理，不自动重试。")
        return 1
    except APIStatusError as error:
        print(f"\n模型服务返回 HTTP {error.status_code}：检查模型名称、余额或服务限流。")
        return 1
    except Exception:
        # 不打印原始异常、请求对象或设置，避免意外泄露密钥。
        print("\n检查未完成：模型输出不完整或 Agent 执行失败；不自动重试。")
        return 1
    print(f"\n检查成功：收到 {count} 段非空回答文字，模型正常结束。")
    if count < 2:
        print("此次只有一段文字，尚未观察到多段增量输出。")
    print("这只是终端测试；网页聊天、SSE 接口及长期记忆尚未接入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
