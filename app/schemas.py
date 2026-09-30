"""请求体模型与校验规则 (非法容量等在此明确报错)。"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field, StrictInt


class PaperIn(BaseModel):
    paper_id: str = Field(min_length=1, max_length=128)
    manuscript: str = ""
    topics: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)


class PaperUpdate(BaseModel):
    manuscript: str = ""
    topics: List[str] = Field(default_factory=list)
    institutions: List[str] = Field(default_factory=list)


class ReviewerIn(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    credential: str = Field(min_length=1, max_length=256)
    topics: List[str] = Field(default_factory=list)
    institution: str = Field(min_length=1, max_length=256)
    capacity: StrictInt = Field(ge=1, le=100000, description="最大可评审论文数, 必须为 >= 1 的整数")
    avoid_papers: List[str] = Field(default_factory=list)


class ReviewerUpdate(BaseModel):
    credential: str = Field(min_length=1, max_length=256)
    topics: List[str] = Field(default_factory=list)
    institution: str = Field(min_length=1, max_length=256)
    capacity: StrictInt = Field(ge=1, le=100000)
    avoid_papers: List[str] = Field(default_factory=list)


class PublishIn(BaseModel):
    base_revision: int = Field(ge=0, description="预演结果中返回的资料修订号")
