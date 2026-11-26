from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Cell = str | int | float | bool | None


class Snapshot(BaseModel):
    columns: list[str]
    rows: list[list[Cell]]
    truncated: bool = False


class Step(BaseModel):
    id: int
    kind: str
    title: str
    explanation: str
    sql: str
    data: Snapshot
    depth: int = 0
    details: list[str] = Field(default_factory=list)
    source_start: int | None = None
    source_end: int | None = None


class Trace(BaseModel):
    query: str
    steps: list[Step]
    plan: dict
    analyze: str | None = None
    mutation: bool = False
    affected_rows: int | None = None
    notices: list[str] = Field(default_factory=list)


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=50_000)
    analyze: bool = False


class SetupRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=200_000)


class ExternalImport(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=64)
    user: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", repr=False)
    query: str = Field(min_length=1, max_length=50_000)
    table: str = Field(min_length=1, max_length=64)


class AIRequest(BaseModel):
    query: str = Field(max_length=50_000)
    step: Step
    audience: Literal["beginner", "intermediate", "advanced"] = "beginner"
