"""请求 / 响应数据模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class DependencyIn(BaseModel):
    id: int = Field(..., description="前置提示 id")
    delay: float = Field(0.0, ge=0, description="相对前置结束的延迟秒数")


class CueBase(BaseModel):
    department: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=120)
    duration: float = Field(..., ge=0, le=100 * 3600)
    locked_start: float | None = Field(None, ge=0, le=24 * 3600)
    sort_order: int = 0
    predecessors: list[DependencyIn] = Field(default_factory=list)

    @field_validator("department", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v

    @field_validator("predecessors")
    @classmethod
    def _unique_preds(cls, v: list[DependencyIn]) -> list[DependencyIn]:
        ids = [d.id for d in v]
        if len(ids) != len(set(ids)):
            raise ValueError("同一条前置依赖不能重复添加")
        return v


class CueCreate(CueBase):
    pass


class CueUpdate(CueBase):
    pass


class RunStart(BaseModel):
    note: str = Field("", max_length=200, description="本场备注（可选）")


class CueAssets(BaseModel):
    asset_ids: list[int] = Field(
        default_factory=list, description="绑定的素材 id（有序、不可重复）")
