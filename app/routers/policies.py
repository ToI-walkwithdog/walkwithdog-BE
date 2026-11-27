"""Public policy document retrieval before login and throughout onboarding."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api_contract import ContractErrorResponse, ContractRoute
from app.policy_documents import active_policy_document


class PolicyDocumentRoute(ContractRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request):
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


class PolicyDocumentData(BaseModel):
    kind: Literal["terms", "privacy"]
    title: str
    document_version: str
    effective_at: str = Field(json_schema_extra={"format": "date-time"})
    content_format: Literal["markdown"] = "markdown"
    content: str


class PolicyDocumentResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: PolicyDocumentData
    message: str = "정책 문서를 조회했습니다."


router = APIRouter(prefix="/policies", tags=["policies"], route_class=PolicyDocumentRoute)


@router.get("/{kind}", response_model=PolicyDocumentResponse, responses={
    422: {"model": ContractErrorResponse, "description": "INVALID_POLICY_KIND"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
    503: {"model": ContractErrorResponse, "description": "POLICY_DOCUMENT_UNAVAILABLE"},
})
def get_policy_document(kind: str) -> PolicyDocumentResponse:
    document = active_policy_document(kind)
    return PolicyDocumentResponse(data=PolicyDocumentData(
        kind=document.kind,
        title=document.title,
        document_version=document.document_version,
        effective_at=document.effective_at.isoformat(),
        content=document.content,
    ))
