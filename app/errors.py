"""AppError and HTTP error envelope compatible with WeKnora clients."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: int = 1000,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        return {"success": False, "error": error}


class AuthError(AppError):
    """Plain authentication-gate error matching Go's middleware responses."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 401,
        code: int = 1001,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code)
        self.extra = extra or {}

    def body(self) -> dict[str, Any]:
        return {"error": self.message, **self.extra}


def auth_failure(
    message: str = "Unauthorized: missing authentication",
    *,
    status: int = 401,
    code: int = 1001,
    extra: dict[str, Any] | None = None,
) -> AuthError:
    return AuthError(message, status=status, code=code, extra=extra)


def unauthorized(message: str = "Unauthorized: missing authentication") -> AppError:
    return AppError(message, status=401, code=1001)


def forbidden(message: str = "Forbidden") -> AppError:
    return AppError(message, status=403, code=1002)


def not_found(message: str = "Not found") -> AppError:
    return AppError(message, status=404, code=1003)


def validation(message: str, details: Any = None) -> AppError:
    return AppError(message, status=400, code=1010, details=details)


def conflict(message: str) -> AppError:
    return AppError(message, status=409, code=1005)


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)
