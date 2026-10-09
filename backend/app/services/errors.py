"""业务异常仅携带公开代号和安全提示；HTTP 状态与 request_id 由未来接口层设置。"""


class EmailAlreadyRegisteredError(Exception):
    code = "EMAIL_ALREADY_REGISTERED"

    def __init__(self) -> None:
        super().__init__("This email address is already registered.")


class RegistrationUnavailableError(Exception):
    code = "REGISTRATION_UNAVAILABLE"

    def __init__(self) -> None:
        # 提交时断线可能无法确定是否已提交，不宣称数据库一定没有新账号。
        super().__init__(
            "Registration could not be confirmed. Please try signing in before registering again."
        )


class InvalidCredentialsError(Exception):
    code = "INVALID_CREDENTIALS"

    def __init__(self) -> None:
        super().__init__("Invalid email or password.")


class LoginUnavailableError(Exception):
    code = "LOGIN_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Login could not be confirmed. Please try again later.")


class AuthenticationRequiredError(Exception):
    code = "UNAUTHENTICATED"

    def __init__(self) -> None:
        super().__init__("Please sign in to continue.")


class AuthenticationUnavailableError(Exception):
    code = "AUTHENTICATION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Authentication could not be checked. Please try again later.")


class LogoutUnavailableError(Exception):
    code = "LOGOUT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Logout could not be confirmed. Please try again later.")


class SessionUnavailableError(Exception):
    code = "SESSION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The session operation could not be confirmed. Please refresh before trying again.")


class SessionNotFoundError(Exception):
    code = "SESSION_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("Session not found or inaccessible.")


class MessageHistoryUnavailableError(Exception):
    code = "MESSAGE_HISTORY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Message history could not be confirmed. Please try again later.")


class SessionBusyError(Exception):
    code = "SESSION_BUSY"

    def __init__(self) -> None:
        super().__init__("This session is still processing a message.")


class IdempotencyConflictError(Exception):
    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self) -> None:
        super().__init__("This message key was already used with different content.")


class MessageSendUnavailableError(Exception):
    code = "MESSAGE_SEND_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Message processing could not be confirmed. Please check the conversation before trying again.")


class ContextTooLargeError(Exception):
    code = "CONTEXT_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("The question and required context exceed the input budget. Please shorten the question.")


class AgentInputUnavailableError(Exception):
    code = "AGENT_INPUT_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Agent input could not be prepared. Please try again later.")
