"""Base for every request and response model."""

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    # Fields with defaults are optional in requests but always present in responses; saying so
    # in the OpenAPI schema gives the generated web types the same guarantee.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
