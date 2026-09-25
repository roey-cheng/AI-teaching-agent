"""通用 HTTP 错误的 JSON 格式；不安装异常处理器，也不决定 HTTP 状态码。"""

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """只接收后端整理过的安全错误信息，不直接包装异常或原始请求。"""

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1)
    # request_id 由后续请求处理代码提供，本类不自动生成。
    # message 使用可公开的英文提示；不把 str(exception) 或请求原文直接塞进来。
    # ignore 只过滤未声明字段，不会识别 message 字符串内部的密码等敏感信息。


class ErrorResponse(BaseModel):
    """普通 HTTP 错误外壳：{"error": {"code": ..., "message": ..., "request_id": ...}}。"""

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    error: ErrorDetail
    # HTTP 401/404/422 等状态码由接口层设置，不另加 status_code JSON 字段。
    # SSE 已开始后的错误继续使用 stream.py 的 MessageErrorData。
