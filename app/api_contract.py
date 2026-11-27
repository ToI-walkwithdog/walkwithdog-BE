"""Route-scoped `code`/`data`/`message` responses for the onboarding contract.

Only routes registered with ContractRoute use this format. Every other API
keeps FastAPI's default `detail` errors, so no global handler is installed.
"""
import logging

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

INTERNAL_SERVER_ERROR_MESSAGE = "일시적인 오류가 발생했습니다."
REQUIRED_FIELD_MESSAGE = "필수 입력 항목을 확인해 주세요."


class ContractErrorResponse(BaseModel):
    code: str
    data: dict
    message: str


class ContractError(Exception):
    def __init__(self, status_code: int, code: str, message: str, data: dict | None = None,
                 headers: dict | None = None):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data or {}
        self.headers = headers

    def response(self) -> JSONResponse:
        return contract_response(self.status_code, self.code, self.message, self.data, self.headers)


def contract_response(status_code: int, code: str, message: str, data: dict | None = None,
                      headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, headers=headers,
                        content={"code": code, "data": data or {}, "message": message})


def ok(data: dict, message: str) -> JSONResponse:
    return contract_response(200, "OK", message, data)


TOO_MANY_REQUESTS_MESSAGE = "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."


def too_many_requests(retry_after_seconds: int, message: str = TOO_MANY_REQUESTS_MESSAGE) -> ContractError:
    """429 contract; app.rate_limit passes the real seconds until the next allowed request."""
    return ContractError(429, "TOO_MANY_REQUESTS", message,
                         {"retry_after_seconds": retry_after_seconds},
                         headers={"Retry-After": str(retry_after_seconds)})


def as_field_error(rule, value):
    """Run a shared rule inside a Pydantic validator, keeping its contract code and data."""
    try:
        return rule(value)
    except ContractError as exc:
        if exc.status_code != 422:
            raise
        raise PydanticCustomError(exc.code, exc.message, {"contract_data": exc.data}) from None


class ContractRoute(APIRoute):
    """Translate request validation, contract errors and unexpected failures for one route."""

    def body_model(self):
        return getattr(self.body_field, "type_", None)

    def first_body_field(self):
        fields = getattr(self.body_model(), "model_fields", None)
        return next(iter(fields), None) if fields else None

    def validation_error(self, exc: RequestValidationError) -> ContractError:
        error = exc.errors()[0]
        data = (error.get("ctx") or {}).get("contract_data")
        if data is not None:
            return ContractError(422, error["type"], error["msg"], data)
        # Missing fields, null or non-string values, and unreadable bodies are all
        # treated as "no usable value". A body model may name a per-field code and
        # message for that case in contract_required_errors; REQUIRED_FIELD otherwise.
        location = error.get("loc", ())
        field = location[1] if len(location) > 1 and isinstance(location[1], str) else None
        field = field or self.first_body_field() or "body"
        required = getattr(self.body_model(), "contract_required_errors", {})
        code, message = required.get(field, ("REQUIRED_FIELD", REQUIRED_FIELD_MESSAGE))
        return ContractError(422, code, message, {"field": field})

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def contract_handler(request):
            try:
                return await handler(request)
            except ContractError as exc:
                return exc.response()
            except RequestValidationError as exc:
                return self.validation_error(exc).response()
            except StarletteHTTPException:
                raise
            except Exception as exc:
                # Log only the exception type: DB errors can carry request values.
                logger.error("%s %s failed: %s", request.method, self.path_format, type(exc).__name__)
                return contract_response(500, "INTERNAL_SERVER_ERROR", INTERNAL_SERVER_ERROR_MESSAGE)

        return contract_handler
