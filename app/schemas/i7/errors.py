"""The single I07 API error shape, and the one place domain exceptions map to
HTTP responses.

Every error the API returns has the same envelope::

    { "error": { "code": "...", "message": "...", "details": {} } }

``code`` is a stable machine-readable string a client can branch on; ``message``
is for a human; ``details`` carries structured context (which recommendation,
which role) without ever including a stack trace or a raw database exception.
"""

from fastapi import HTTPException
from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = {}


class ErrorResponse(BaseModel):
    error: ErrorBody


class ApiError(HTTPException):
    """Raise this from a route or a dependency; FastAPI renders it through
    ``ErrorResponse`` via the handler registered in ``app.api.i7.router``."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.error_details = details or {}
        super().__init__(status_code=status_code, detail=message)


def not_found(code: str, message: str, **details: object) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str, **details: object) -> ApiError:
    return ApiError(409, code, message, details)


def bad_request(code: str, message: str, **details: object) -> ApiError:
    return ApiError(400, code, message, details)


def forbidden(code: str, message: str, **details: object) -> ApiError:
    return ApiError(403, code, message, details)
