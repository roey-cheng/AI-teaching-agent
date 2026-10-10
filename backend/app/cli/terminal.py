"""终端输入输出：隐藏密码、分开显示事件，不让模型文字执行终端控制序列。"""

import getpass
import sys
import warnings
from typing import TextIO

from app.schemas import MessageHistoryResponse
from app.services.chat_execution import ChatEvent


def terminal_text(value: str) -> str:
    # ESC、回车、退格及 C1 控制码都移除；保留换行/tab。即使 ANSI 被分成多个片段也不能执行。
    # 只处理显示，不修改发给模型或保存到数据库的原文。
    return "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))


class Terminal:
    def __init__(self, output: TextIO | None = None):
        self.output = output if output is not None else sys.stdout

    def write(self, text: str, *, end: str = "\n") -> None:
        self.output.write(terminal_text(text) + end)
        self.output.flush()

    def read(self, prompt: str) -> str:
        return input(prompt)

    def secret(self, prompt: str) -> str:
        # 没有安全 TTY 时禁止 getpass 退化为可见密码输入。
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)


class EventPrinter:
    STAGES = {
        "context_ready": "Conversation context is ready.",
        "agent_running": "Agent is running; waiting for model output.",
        "thinking": "Receiving model reasoning.",
        "answering": "Receiving the answer.",
        "saving": "Saving the complete answer.",
    }

    def __init__(self, terminal: Terminal):
        self.terminal = terminal
        self.section: str | None = None

    def line(self, text: str) -> None:
        if self.section is not None:
            self.terminal.write("")
            self.section = None
        self.terminal.write(text)

    async def __call__(self, item: ChatEvent) -> None:
        if item.event_name == "message_start":
            self.line(f"[Started] Session {item.session_id}; question {item.user_message_id}; attempt {item.attempt_id}")
        elif item.event_name == "agent_progress":
            self.line(f"[Progress] {self.STAGES[item.stage]}")
        elif item.event_name in ("reasoning_delta", "message_delta"):
            section = "Thinking" if item.event_name == "reasoning_delta" else "Answer"
            if self.section != section:
                self.line(f"[{section}]")
                self.section = section
            self.terminal.write(item.text, end="")
        elif item.event_name == "message_done":
            # 正文已经逐段打印；此处不再重复整篇回答。
            self.line(f"[Saved] Answer {item.assistant_message.message_id}. Use /history to read it from MySQL.")
        elif item.event_name == "message_error":
            self.line(f"[Failed] {item.error.code}: {item.error.message}")
            self.terminal.write("Any partial answer above was not saved as a completed reply. Retry is not available in this CLI yet.")


def print_history(terminal: Terminal, history: MessageHistoryResponse) -> None:
    terminal.write(f"[History] Session {history.session_id}; busy={history.is_generating}")
    if not history.items:
        terminal.write("No saved messages yet.")
    for item in history.items:
        terminal.write(f"[{item.role} #{item.message_id}]")
        terminal.write(item.content)
        if item.role == "USER":
            terminal.write(f"[Generation] {item.generation.status}")
            if item.generation.error is not None:
                terminal.write(f"{item.generation.error.code}: {item.generation.error.message}")
    terminal.write("Reasoning and progress are live-only; they are not stored in chat history.")
