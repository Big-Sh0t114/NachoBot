import re
from typing import Any, Iterable


# 常见Error Code Mapping (以OpenAI API为例)
error_code_mapping = {
    400: "参数不正确",
    401: "API-Key错误，认证失败，请检查/config/model_list.toml中的配置是否正确",
    402: "账号余额不足",
    403: "模型拒绝访问，可能需要实名或余额不足",
    404: "Not Found",
    413: "请求体过大，请尝试压缩图片或减少输入内容",
    429: "请求过于频繁，请稍后再试",
    500: "服务器内部故障",
    503: "服务器负载过高",
}


_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s\"'&,;]+")
_SK_TOKEN_PATTERN = re.compile(r"(?i)\bsk-[a-z0-9_-]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)[\"']?\b(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|token|secret|password|credential)"
    r"[\"']?(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,;}]+)"
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")


def sanitize_error_text(value: Any, sensitive_values: Iterable[str] = ()) -> str | None:
    """Redact common credential forms and caller-supplied exact secrets."""
    if value is None:
        return None
    text = str(value)
    for secret in sensitive_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return _SK_TOKEN_PATTERN.sub("[REDACTED]", text)


class NetworkConnectionError(Exception):
    """连接异常，常见于网络问题或服务器不可用"""

    def __init__(self):
        super().__init__()

    def __str__(self):
        return "连接异常，请检查网络连接状态或URL是否正确"


class ReqAbortException(Exception):
    """请求异常退出，常见于请求被中断或取消"""

    def __init__(self, message: str | None = None):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return self.message or "请求因未知原因异常终止"


class RespNotOkException(Exception):
    """请求响应异常，见于请求未能成功响应（非 '200 OK'）"""

    def __init__(
        self,
        status_code: int,
        message: str | None = None,
        error_code: str | int | None = None,
        param: str | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
    ):
        safe_message = sanitize_error_text(message)
        safe_code = sanitize_error_text(error_code)
        safe_param = sanitize_error_text(param)
        safe_type = sanitize_error_text(error_type)
        super().__init__(safe_message)
        self.status_code = status_code
        self.message = safe_message
        self.error_code = safe_code
        self.param = safe_param
        self.error_type = safe_type
        self.request_id = sanitize_error_text(request_id)

    def __str__(self):
        summary = error_code_mapping.get(self.status_code, "未知的异常响应")
        details = [f"HTTP {self.status_code}: {summary}"]
        if self.message:
            details.append(self.message)
        metadata = []
        if self.error_code is not None:
            metadata.append(f"code={self.error_code}")
        if self.param is not None:
            metadata.append(f"param={self.param}")
        if self.error_type is not None:
            metadata.append(f"type={self.error_type}")
        if self.request_id is not None:
            metadata.append(f"request_id={self.request_id}")
        if metadata:
            details.append("(" + ", ".join(metadata) + ")")
        return " ".join(details)


class RespParseException(Exception):
    """响应解析错误，常见于响应格式不正确或解析方法不匹配"""

    def __init__(self, ext_info: Any, message: str | None = None):
        super().__init__(message)
        self.ext_info = ext_info
        self.message = message

    def __str__(self):
        return self.message or "解析响应内容时发生未知错误，请检查是否配置了正确的解析方法"


class EmptyResponseException(Exception):
    """响应内容为空"""

    def __init__(self, message: str = "响应内容为空，这可能是一个临时性问题"):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return self.message


class ModelAttemptFailed(Exception):
    """当在单个模型上的所有重试都失败后，由“执行者”函数抛出，以通知“调度器”切换模型。"""

    def __init__(self, message: str, original_exception: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.original_exception = original_exception

    def __str__(self):
        return self.message
