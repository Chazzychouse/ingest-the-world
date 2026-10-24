"""Request and response shapes for collections and grants. Validation lives here, once."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from ingest_api.schema import ApiModel

COLLECTION_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
# Postgres text can't hold NUL, so free text rejects it at the edge instead of failing later.
NO_NUL_PATTERN = r"^[^\x00]*$"
MAX_DAYS = 36500
MAX_LIST_ITEMS = 32

CollectionId = Annotated[str, Field(pattern=COLLECTION_ID_PATTERN)]
Code = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
Codes = Annotated[list[Code], Field(max_length=MAX_LIST_ITEMS)]
Grantee = Annotated[str, Field(min_length=1, max_length=256, pattern=NO_NUL_PATTERN)]
PrincipalType = Literal["role", "group", "user"]
Access = Literal["read", "write", "curate"]


class TierStep(ApiModel):
    after_days: int = Field(gt=0, le=MAX_DAYS)
    tier: Literal["cool", "cold", "archive"]


class Retention(ApiModel):
    retain_days: int | None = Field(default=None, gt=0, le=MAX_DAYS)
    tiering: list[TierStep] = Field(default_factory=list[TierStep], max_length=MAX_LIST_ITEMS)
    legal_hold: bool = False


class CollectionIn(ApiModel):
    id: CollectionId
    title: str = Field(min_length=1, max_length=200, pattern=NO_NUL_PATTERN)
    description: str = Field(default="", max_length=4000, pattern=NO_NUL_PATTERN)
    marking: Code = "UNCLASSIFIED"
    cui_categories: Codes = Field(default_factory=list[str])
    dissemination: Codes = Field(default_factory=list[str])
    retention: Retention = Field(default_factory=Retention)

    @model_validator(mode="after")
    def cui_details_need_cui(self) -> "CollectionIn":
        if self.marking != "CUI" and (self.cui_categories or self.dissemination):
            raise ValueError("cui_categories and dissemination apply only to CUI collections")
        return self


class Collection(CollectionIn):
    """A collection as returned by the API; built straight from `store.CollectionRow`."""

    model_config = ConfigDict(from_attributes=True)  # merged with ApiModel's config

    marking_label: str
    marking_color: str
    marking_rank: int
    created_by: str
    created_at: datetime


class GrantIn(ApiModel):
    access: Access
