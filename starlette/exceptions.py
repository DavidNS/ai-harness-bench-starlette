from __future__ import annotations

import http
from collections.abc import Mapping
from typing import Any


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str | None = None, headers: Mapping[str, str] | None = None) -> None:
        if detail is None:
            detail = http.HTTPStatus(status_code).phrase
        self.status_code = status_code
        self.detail = detail
        self.headers = headers

    def __str__(self) -> str:
        return f"{self.status_code}: {self.detail}"

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(status_code={self.status_code!r}, detail={self.detail!r})"


class WebSocketException(Exception):
    def __init__(self, code: int, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason or ""

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(code={self.code!r}, reason={self.reason!r})"


class StarletteDeprecationWarning(UserWarning):
    """A custom deprecation warning for Starlette.

    Unlike the built-in DeprecationWarning, this inherits from UserWarning to ensure it is visible by default, helping
    users discover deprecated features without needing to enable warnings explicitly.

    Reference: https://sethmlarson.dev/deprecations-via-warnings-dont-work-for-python-libraries
    """


class RequestBodyTooLarge(BaseException):
    """
    Raised from `receive()` once a request body crosses the applicable limit.

    A `BaseException` so that error handling middleware inside the limit layer does not
    turn it into a 500: the layer that tracks the request answers with the 413.
    """

    def __init__(self, state: Any = None, receive: Any = None) -> None:
        super().__init__("Request body exceeds the maximum allowed size.")
        self.state = state
        # The server's own `receive`, which can still be polled for a disconnect.
        self.receive = receive
