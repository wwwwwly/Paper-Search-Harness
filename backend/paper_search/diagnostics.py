"""Small shared diagnostic values; no logging backend or tracing framework."""

import asyncio
import re
import traceback
from contextvars import ContextVar

from paper_search.domain import DeadlineExceeded, ExecutionError, ModelUnavailable

current_tool_call: ContextVar[str | None] = ContextVar("tool_call_id", default=None)


def execution_error(exc: BaseException) -> ExecutionError:
    # Exception messages may contain authenticated URLs or SDK headers.
    message = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[redacted]", str(exc))
    message = re.sub(
        r"(?i)((?:api[_-]?key|token|authorization|password)\s*[=:]\s*)[^\s&,;]+",
        r"\1[redacted]",
        message,
    )
    code = "execution_error"
    if isinstance(exc, asyncio.CancelledError):
        code, message = "cancelled", "调用被取消（用户取消、任务中断或执行期限到达）"
    elif isinstance(exc, TimeoutError):
        code, message = "timeout", message or "调用超过执行期限"
    elif isinstance(exc, DeadlineExceeded):
        code = "deadline_reached"
    elif isinstance(exc, ModelUnavailable):
        code = exc.code
    return ExecutionError(
        code=code,
        message=message[:1200] or type(exc).__name__,
        exception_type=type(exc).__name__,
        retryable=bool(getattr(exc, "retryable", False)),
        http_status=getattr(exc, "status", None),
        finish_reason=getattr(exc, "finish_reason", None),
        provider_request_id=getattr(exc, "request_id", None),
        stack=tuple(
            f"{frame.filename}:{frame.lineno} in {frame.name}"
            for frame in traceback.extract_tb(exc.__traceback__)[-12:]
        ),
    )
