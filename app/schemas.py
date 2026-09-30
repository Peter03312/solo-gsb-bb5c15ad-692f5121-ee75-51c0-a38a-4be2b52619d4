"""请求体模型与校验规则 (非法容量等在此明确报错)。"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, StrictInt, model_validator


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


class DecisionIn(BaseModel):
    """评审人对当前发布方案中本人分配的决定: 确认或回避 (回避须给出非空原因)。"""

    paper_id: str = Field(min_length=1, max_length=128)
    decision: Literal["confirm", "decline"]
    reason: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_required_for_decline(self):
        if self.decision == "decline" and (self.reason is None or not self.reason.strip()):
            raise ValueError("回避必须提交非空原因 (reason)")
        return self


class ReassignPublishIn(BaseModel):
    """补位发布: 须同时复核资料修订号与发布版序号, 任一过期即拒绝。"""

    base_revision: int = Field(ge=0, description="补位预演返回的资料修订号")
    base_published_version: int = Field(ge=1, description="补位预演返回的发布版序号")
