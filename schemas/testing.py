# schemas/testing.py

from typing import Literal
from pydantic import BaseModel, Field


class Issue(BaseModel):
    id: str

    area: Literal[
        "frontend",
        "backend",
        "integration",
        "specification"
    ]

    severity: Literal[
        "low",
        "medium",
        "high",
        "critical"
    ]

    description: str
    expected: str
    actual: str
    recommendation: str


class TestReport(BaseModel):
    passed: bool

    tests_executed: int
    tests_passed: int
    tests_failed: int

    issues: list[Issue] = Field(default_factory=list)