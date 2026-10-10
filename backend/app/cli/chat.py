"""真实模型终端聊天入口：python -m app.cli.chat；不启动网页、不另写聊天业务。"""

import argparse
import asyncio
from pathlib import Path
import sys
from uuid import uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import Engine, inspect

from app.agent.input_policy import policy_for_model
from app.cli.terminal import EventPrinter, Terminal, print_history
from app.core.config import (
    DatabaseSettings, ModelSettings, TracingSettings,
    load_database_settings, load_model_settings, load_tracing_settings,
)
from app.db.base import Base
from app.db.engine import build_database_engine
from app.db.session import build_session_factory
from app.schemas import DuplicateMessageResponse, LoginRequest, RegisterRequest, SendMessageRequest
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.chat_execution import execute_chat_turn
from app.services.chat_sessions import create_chat_session
from app.services.errors import (
    ContextTooLargeError, EmailAlreadyRegisteredError, GenerationRateLimitError, InvalidCredentialsError,
)
from app.services.generation_limit import GenerationRateLimiter
from app.services.generation_registry import GenerationRegistry
from app.services.login import login_user
from app.services.logout import logout_user
from app.services.message_history import get_message_history

HELP = (
    "Type one line to send a real model request. Commands:\n"
    "  /new      Start a new conversation\n"
    "  /history  Read this conversation from MySQL\n"
    "  /help     Show this help\n"
    "  /quit     Log out of this terminal and exit\n"
    "Ctrl+C cancels the current turn and exits after cleanup. Wait for cleanup; do not press it repeatedly.\n"
    "No old-session selection, multiline editor, or failed-reply retry in this CLI yet."
)


class DatabaseNotReadyError(Exception):
    pass


def check_database_ready(engine: Engine) -> None:
    """只读：核对 MySQL、迁移头版本和表存在；不执行迁移、不自动清理旧任务。"""
    backend = Path(__file__).resolve().parents[2]
    heads = set(ScriptDirectory.from_config(Config(str(backend / "alembic.ini"))).get_heads())
    with engine.connect() as connection:
        dialect = connection.dialect
        if (dialect.name != "mysql" or getattr(dialect, "is_mariadb", False)
                or (dialect.server_version_info or ()) < (8, 4)):
            raise DatabaseNotReadyError()
        actual = set(MigrationContext.configure(connection).get_current_heads())
        tables = set(inspect(connection).get_table_names())
        if actual != heads or not set(Base.metadata.tables).issubset(tables):
            raise DatabaseNotReadyError()


def authenticate(terminal: Terminal, factory):
    """注册后仍走正常登录；不直接用 user_id 假装身份，不持久化终端原始凭据。"""
    failures = 0
    while failures < 3:
        choice = terminal.read("[l] Login / [r] Register / [q] Quit: ").strip().lower()
        if choice in ("q", "quit"):
            return None
        if choice not in ("l", "login", "r", "register"):
            terminal.write("Choose l, r, or q.")
            continue
        email = terminal.read("Email: ")
        password = terminal.secret("Password (hidden): ")
        try:
            if choice in ("r", "register"):
                confirmation = terminal.secret("Confirm password (hidden): ")
                if password != confirmation:
                    terminal.write("Passwords do not match.")
                    continue
                name = terminal.read("Display name: ")
                register_user(RegisterRequest(email=email, password=password, display_name=name), factory)
                terminal.write("Account created. Signing in...")
            result = login_user(LoginRequest(email=email, password=password), factory)
            return result
        except ValidationError:
            terminal.write("Invalid account input. Check email, password length, and display name.")
        except EmailAlreadyRegisteredError:
            terminal.write("This email is already registered. Choose login instead.")
        except InvalidCredentialsError:
            failures += 1
            terminal.write(f"Invalid email or password. Failed login {failures}/3.")
        finally:
            # 不在对象、日志或文件中保留密码；这不是安全擦除 Python 内存的保证。
            password = None
            confirmation = None
    terminal.write("Three failed logins. Exiting.")
    return None


def run_chat(
    terminal: Terminal, factory, model_settings: ModelSettings, tracing_settings: TracingSettings,
) -> int:
    """同步终端循环 + 持续复用的 Runner。等待键盘时没有 Agent 在后台运行。

    input/getpass 不放进异步线程，避免 Ctrl+C 后还有线程挂在输入上。
    模型运行期间由 Runner 取消主协程，execute_chat_turn 完成自己的安全收尾。
    """
    token = None
    code = 0
    try:
        policy = policy_for_model(model_settings.name)
        login = authenticate(terminal, factory)
        if login is None:
            return 0
        token = login.token
        user = get_current_user(token, factory)
        chat = create_chat_session(user, factory)
        terminal.write(f"Signed in as {user.display_name}. New session: {chat.session_id}")
        terminal.write(HELP)
        registry = GenerationRegistry()
        limiter = GenerationRateLimiter()
        printer = EventPrinter(terminal)
        # 提交结果不明后暂停发送；保留原 key，不自动以新 key 重发。
        uncertain_request: SendMessageRequest | None = None
        with asyncio.Runner() as runner:
            while True:
                line = terminal.read("\nYou> ")
                command = line.strip()
                if command == "/quit":
                    break
                if command == "/help":
                    terminal.write(HELP)
                    continue
                if not command:
                    terminal.write("Empty messages are not sent.")
                    continue
                if command.startswith("/") and command not in ("/new", "/history"):
                    terminal.write("Unknown command. Use /help; nothing was sent to the model.")
                    continue
                user = get_current_user(token, factory)  # 每次真实业务操作都重新检查身份。
                if command == "/history":
                    history = get_message_history(user, chat.session_id, factory, registry)
                    print_history(terminal, history)
                    if uncertain_request is not None:
                        if history.is_generating:
                            terminal.write("The conversation is still busy. Sending remains disabled.")
                        else:
                            terminal.write("History was re-read. No request will be resent automatically; any new text is a NEW question.")
                            uncertain_request = None
                    continue
                if command == "/new":
                    if uncertain_request is not None:
                        terminal.write("Check /history first. This conversation has an unconfirmed result.")
                        continue
                    chat = create_chat_session(user, factory)
                    terminal.write(f"New session: {chat.session_id}")
                    continue
                if uncertain_request is not None:
                    terminal.write("Sending is paused. Use /history to confirm the last request before sending again.")
                    continue
                try:
                    request = SendMessageRequest(client_message_key=str(uuid4()), content=line)
                except ValidationError:
                    terminal.write("A message must contain text and be at most 20,000 characters.")
                    continue
                try:
                    result = runner.run(execute_chat_turn(
                        user, chat.session_id, request, factory, registry,
                        model_settings=model_settings, tracing_settings=tracing_settings,
                        input_policy=policy, check_new_message=lambda _request: limiter.check(user.user_id),
                        on_event=printer,
                    ))
                    if isinstance(result, DuplicateMessageResponse):
                        printer.line(f"[Duplicate] Existing attempt {result.attempt_id}: {result.status}. Use /history.")
                except GenerationRateLimitError as error:
                    printer.line(f"[RATE_LIMITED] Wait at least {error.retry_after_seconds} seconds before sending again.")
                except ContextTooLargeError:
                    printer.line("[CONTEXT_TOO_LARGE] Please shorten the question. It was not accepted.")
                except Exception:
                    # 不打印原始异常或盲目重发；历史无法核对时外层报错退出。
                    uncertain_request = request
                    printer.line("[Unconfirmed] Check /history before sending again. The request will not be resent automatically.")
    except (EOFError, KeyboardInterrupt):
        terminal.write("\nExiting after execution cleanup. Saved chat history is kept.")
    except Exception:
        terminal.write("Chat could not continue. Check configuration, login status, database availability, and saved history. No automatic retry.")
        code = 1
    finally:
        if token is not None:
            try:
                logout_user(token, factory)
                terminal.write("This terminal login has been revoked. Other logins and saved conversations are unchanged.")
            except Exception:
                terminal.write("Logout could not be confirmed. The terminal credential was not written to disk; its database expiry still applies.")
                code = 1
    return code


def launch(
    terminal: Terminal, database: DatabaseSettings, model: ModelSettings, tracing: TracingSettings,
) -> int:
    """配置由 main 加载；测试传假的配置，避免误读用户 .env 或调用真实模型。"""
    engine = None
    try:
        policy_for_model(model.name)  # 型号不受支持时，不先创建账号或会话。
        engine = build_database_engine(database)
        check_database_ready(engine)
        terminal.write(f"Database: {database.name} at {database.host}:{database.port}")
        terminal.write(f"Model: {model.name}. Real model requests may incur API charges.")
        terminal.write("Accounts, sessions, questions, and final answers will be saved to this database and will NOT be deleted on exit.")
        if tracing.tracing:
            terminal.write(f"LangSmith is ON: prompts, memory, reasoning, and answers may be uploaded to project {tracing.project}.")
        else:
            terminal.write("LangSmith is OFF.")
        terminal.write("Local single-process tool only. Stop other chat-service/CLI processes using this database before continuing.")
        if terminal.read("Continue with this database and real model? [y/N]: ").strip().lower() not in ("y", "yes"):
            terminal.write("Cancelled. No account, conversation, or model request was created.")
            return 0
        return run_chat(terminal, build_session_factory(engine), model, tracing)
    except DatabaseNotReadyError:
        terminal.write("Database is not ready: MySQL 8.4+ and the current project migrations are required. Check Alembic status; no migration was run.")
        return 1
    except (EOFError, KeyboardInterrupt):
        terminal.write("\nCancelled.")
        return 0
    except Exception:
        terminal.write("Startup failed. Check DB_* / MODEL_* / LANGSMITH_* settings, MySQL availability, and migrations. Do not share secrets.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def main() -> int:
    argparse.ArgumentParser(description="Interactive real-model chat using the existing backend and MySQL. No web server is started.").parse_args()
    terminal = Terminal()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        terminal.write("Run this command in an interactive terminal. Piped credentials and non-interactive input are not supported.")
        return 1
    try:
        database, model, tracing = load_database_settings(), load_model_settings(), load_tracing_settings()
    except Exception:
        terminal.write("Invalid backend configuration. Check DB_* / MODEL_* / LANGSMITH_* in backend/.env; do not share API keys.")
        return 1
    return launch(terminal, database, model, tracing)


if __name__ == "__main__":
    raise SystemExit(main())
