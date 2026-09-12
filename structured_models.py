"""Strict Pydantic v2 response contracts for DeepSeek JSON tasks."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplanationBlock(StrictModel):
    section: Literal["analysis", "answerBasis", "pitfalls", "clinicalNotes"]
    type: Literal["paragraph", "list", "table", "callout"]
    title: str = Field(default="", max_length=80)
    text: str | None = None
    items: list[str] | None = None
    columns: list[str] | None = None
    rows: list[list[str]] | None = None
    tone: Literal["info", "important", "warning"] | None = None


class EvidenceContext(StrictModel):
    evidenceId: str
    contextSummary: str = Field(default="", max_length=500)
    excerpt: str | None = None
    highlights: list[str] | None = None


class ExplanationPackageResponse(StrictModel):
    explanationBlocks: list[ExplanationBlock] = Field(min_length=1, max_length=8)
    briefExplanation: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=3)
    suggestedTags: list[str] = Field(default_factory=list, max_length=3)
    knowledgePoints: list[str] = Field(default_factory=list, max_length=3)
    evidenceContext: EvidenceContext | None = None
    evidenceExcerpts: list[EvidenceContext] | None = None


class TagItem(StrictModel):
    id: str
    tags: list[str] = Field(default_factory=list, max_length=3)
    briefExplanation: str = Field(default="", max_length=200)


class TagBatchResponse(StrictModel):
    items: list[TagItem]


class StudyPoint(StrictModel):
    title: str = Field(min_length=1, max_length=40)
    body: str = Field(min_length=1, max_length=1200)


class StudyPointItem(StrictModel):
    id: str
    studyPoints: list[StudyPoint] | str = Field(default_factory=list)


class StudyPointBatchResponse(StrictModel):
    items: list[StudyPointItem]


class MemoryCard(StrictModel):
    title: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=2000)
    memoryCue: str = Field(default="", max_length=500)
    contrast: str = Field(default="", max_length=1000)


class MemoryCardItem(StrictModel):
    id: str
    memoryCards: list[MemoryCard] = Field(min_length=1, max_length=3)


class MemoryCardBatchResponse(StrictModel):
    items: list[MemoryCardItem]


class EvidenceSelectionResponse(StrictModel):
    evidenceId: str
    contextSummary: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=500)
    highlights: list[str] = Field(default_factory=list, max_length=8)


class SubjectSelectionResponse(StrictModel):
    subjects: list[str] = Field(default_factory=list, max_length=3)


class QuestionOrganizeItem(StrictModel):
    id: str = ""
    bank: str = ""
    type: Literal["A1", "A2", "A3", "multiple", "judge", "fill"] = "A1"
    subject: str = ""
    system: str = ""
    difficulty: str = ""
    caseInfo: str = ""
    question: str
    options: list[str] = Field(default_factory=list)
    answer: str | list[list[str]] = ""
    explanation: str = ""
    knowledgePoint: str = ""
    year: str = ""


class QuestionOrganizeResponse(StrictModel):
    items: list[QuestionOrganizeItem]


def instructor_schema(model: type[BaseModel]) -> dict:
    """Use Instructor's schema adapter while retaining raw completions and traces."""
    try:
        import instructor
        adapter = instructor.openai_schema(model)
        return dict(getattr(adapter, "openai_schema"))
    except Exception:
        return model.model_json_schema()
