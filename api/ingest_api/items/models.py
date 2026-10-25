"""STAC-shaped request and response models for items and search (spec §6, D7).

An item is a STAC 1.1 Item: a GeoJSON Feature whose `properties` carry the header metadata the
pipeline extracted, plus core fields that always win over anything a header said. A search is
an ItemCollection whose `next` link carries the cursor for the following page.
"""

import base64
import binascii
import datetime as dt
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from ingest_api.collections.models import NO_NUL_PATTERN, CollectionId
from ingest_api.items.kinds import Kind
from ingest_api.items.metadata import Status
from ingest_api.schema import ApiModel

STAC_VERSION = "1.1.0"
# The extensions whose fields every item may carry and whose schemas they satisfy. Format
# fields (`raster:`, `pc:`, `table:`, `mesh:`) are listed once their schemas are checked.
FILE_EXTENSION = "https://stac-extensions.github.io/file/v2.1.0/schema.json"
PROJECTION_EXTENSION = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"

DEFAULT_PAGE = 20
MAX_PAGE = 100
MAX_FILTER_VALUES = 100
MAX_TERMS = 8
MAX_QUERY_LENGTH = 256
# A query geometry is a filter, not a dataset: big enough for any drawn area of interest.
MAX_POSITIONS = 10_000
MAX_RINGS = 64
MAX_TOKEN_LENGTH = 128
OPEN = ".."

Longitude = Annotated[float, Field(ge=-180, le=180)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Position = tuple[Longitude, Latitude]
Ring = Annotated[list[Position], Field(min_length=4, max_length=MAX_POSITIONS)]
Rings = Annotated[list[Ring], Field(min_length=1, max_length=MAX_RINGS)]
# `[west, south, east, north]`; west > east crosses the antimeridian.
BboxQuery = tuple[Longitude, Latitude, Longitude, Latitude]


def _check_rings(rings: list[list[Position]]) -> int:
    """The number of positions, after checking each ring closes on itself."""
    for ring in rings:
        if ring[0] != ring[-1]:
            raise ValueError("a polygon ring must end where it starts")
    return sum(len(ring) for ring in rings)


def _check_positions(count: int) -> None:
    if count > MAX_POSITIONS:
        raise ValueError(f"a query geometry has at most {MAX_POSITIONS} positions")


class Point(ApiModel):
    type: Literal["Point"]
    coordinates: Position


class Polygon(ApiModel):
    type: Literal["Polygon"]
    coordinates: Rings

    @field_validator("coordinates")
    @classmethod
    def _closed(cls, rings: list[list[Position]]) -> list[list[Position]]:
        _check_positions(_check_rings(rings))
        return rings


class MultiPolygon(ApiModel):
    type: Literal["MultiPolygon"]
    coordinates: Annotated[list[Rings], Field(min_length=1, max_length=MAX_RINGS)]

    @field_validator("coordinates")
    @classmethod
    def _closed(cls, polygons: list[list[list[Position]]]) -> list[list[list[Position]]]:
        _check_positions(sum(_check_rings(rings) for rings in polygons))
        return polygons


Geometry = Annotated[Point | Polygon | MultiPolygon, Field(discriminator="type")]


def _instant(text: str) -> dt.datetime | None:
    if text == OPEN:
        return None
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not an RFC 3339 date-time") from None
    if moment.tzinfo is None:
        raise ValueError(f"{text!r} has no UTC offset")
    return moment


def parse_interval(text: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    """An RFC 3339 instant, or `start/end` with `..` for an open end, as inclusive bounds."""
    parts = text.split("/")
    if len(parts) == 1:
        moment = _instant(text)
        if moment is None:
            raise ValueError("an instant can't be open")
        return moment, moment
    if len(parts) != 2:
        raise ValueError("an interval is start/end")
    start, end = _instant(parts[0]), _instant(parts[1])
    if start is not None and end is not None and start > end:
        raise ValueError("an interval can't end before it starts")
    return start, end


class Cursor(ApiModel):
    """A position in the search order: the last item of a page. Not a secret: it names an item
    its holder has already seen, and every page is filtered afresh (ACC-1)."""

    created_at: dt.datetime
    id: UUID

    def token(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def from_token(cls, token: str) -> Self:
        try:
            return cls.model_validate_json(base64.urlsafe_b64decode(token.encode()))
        except (binascii.Error, ValueError):
            raise ValueError("not a search token") from None


class SearchIn(ApiModel):
    """Every filter given must hold for every result (SRCH-1); a list filter that is empty
    matches nothing."""

    bbox: BboxQuery | None = None
    intersects: Geometry | None = None
    datetime: str | None = Field(default=None, max_length=80)
    q: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH, pattern=NO_NUL_PATTERN)
    collections: list[CollectionId] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    kinds: list[Kind] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    ids: list[UUID] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    token: str | None = Field(default=None, max_length=MAX_TOKEN_LENGTH)

    @field_validator("bbox")
    @classmethod
    def _south_of_north(cls, bbox: BboxQuery | None) -> BboxQuery | None:
        if bbox is not None and bbox[1] > bbox[3]:
            raise ValueError("a bbox's south edge can't be north of its north edge")
        return bbox

    @field_validator("datetime")
    @classmethod
    def _interval(cls, text: str | None) -> str | None:
        if text is not None:
            parse_interval(text)
        return text

    @field_validator("q")
    @classmethod
    def _terms(cls, q: str | None) -> str | None:
        if q is not None and len(q.split()) > MAX_TERMS:
            raise ValueError(f"at most {MAX_TERMS} search terms")
        return q

    @field_validator("token")
    @classmethod
    def _token(cls, token: str | None) -> str | None:
        if token is not None:
            Cursor.from_token(token)
        return token

    @model_validator(mode="after")
    def _one_geometry(self) -> Self:
        if self.bbox is not None and self.intersects is not None:
            raise ValueError("give bbox or intersects, not both")
        return self


class Link(ApiModel):
    rel: str
    href: str
    type: str


class NextBody(ApiModel):
    token: str


class NextLink(ApiModel):
    """STAC's link to the next page of a POST search: repeat the request with `body` merged
    into it."""

    rel: Literal["next"] = "next"
    href: str
    type: Literal["application/geo+json"] = "application/geo+json"
    method: Literal["POST"] = "POST"
    body: NextBody
    merge: Literal[True] = True


class Asset(ApiModel):
    href: str
    type: str
    title: str
    roles: list[str]


class ItemProperties(ApiModel):
    """Core fields plus whatever the header said (`proj:shape`, `raster:bands`, `pc:count`,
    `mesh:triangles`, ...), which is why other keys are allowed."""

    model_config = ConfigDict(extra="allow", validate_by_name=True, serialize_by_alias=True)

    # When the data was captured, once headers say so; until then, when it was cataloged.
    datetime: dt.datetime
    created: dt.datetime
    title: str
    keywords: list[str]
    kind: Kind = Field(alias="ingest:kind")
    media_type: str = Field(alias="ingest:media_type")
    marking: str = Field(alias="ingest:marking")
    marking_rank: int = Field(alias="ingest:marking_rank")
    extraction: Status = Field(alias="ingest:extraction")
    file_size: int = Field(alias="file:size")
    file_checksum: str = Field(alias="file:checksum")  # a multihash: 0x12 (sha2-256), 0x20 bytes
    proj_code: str | None = Field(alias="proj:code")
    proj_wkt2: str | None = Field(alias="proj:wkt2")


class StacItem(ApiModel):
    type: Literal["Feature"] = "Feature"
    stac_version: Literal["1.1.0"] = STAC_VERSION
    stac_extensions: list[str]
    id: UUID
    collection: str
    geometry: Polygon | None
    # STAC requires a bbox with a geometry and forbids one without.
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, exclude_if=lambda bbox: bbox is None
    )
    properties: ItemProperties
    links: list[Link]
    assets: dict[str, Asset]  # empty until downloads are signed (a later slice)


class ItemCollection(ApiModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[StacItem]
    links: list[NextLink]  # empty on the last page (SRCH-2)
    number_returned: int = Field(alias="numberReturned")

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)
