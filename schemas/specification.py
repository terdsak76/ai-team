# schemas/specification.py

from pydantic import BaseModel, Field


class APIEndpoint(BaseModel):
    method: str
    path: str
    description: str
    request_model: str | None = None
    response_model: str | None = None


class TestCase(BaseModel):
    id: str
    description: str
    expected_result: str


class FeatureSpecification(BaseModel):
    feature_name: str
    summary: str

    functional_requirements: list[str]
    acceptance_criteria: list[str]

    frontend_tasks: list[str]
    backend_tasks: list[str]

    api_endpoints: list[APIEndpoint] = Field(default_factory=list)

    test_cases: list[TestCase] = Field(default_factory=list)

    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)