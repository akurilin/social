import json
import re
from pydantic import BaseModel, Field, field_validator, model_validator


SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SourceEditorInput(BaseModel):
    id: str
    title: str
    url: str
    priority: int = Field(ge=1, le=3)
    geo: str = ""
    parse_hint: str = ""
    retrieval_profile: str
    enabled: bool = True
    disabled_reason: str = ""
    extra_json: str = "{}"

    @field_validator("id", "title", "retrieval_profile", mode="before")
    @classmethod
    def strip_required(cls, value):
        value = str(value or "").strip()
        if not value:
            raise ValueError("This field is required.")
        return value

    @field_validator("id")
    @classmethod
    def validate_id(cls, value):
        if not SOURCE_ID.match(value):
            raise ValueError("Use lowercase letters, numbers, and hyphens.")
        return value

    @field_validator("url", "geo", "parse_hint", "disabled_reason", mode="before")
    @classmethod
    def strip_optional(cls, value):
        return str(value or "").strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("The URL must start with http:// or https://.")
        return value

    @field_validator("extra_json")
    @classmethod
    def validate_extra(cls, value):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError as error:
            raise ValueError("Extra fields must be valid JSON: {}".format(error.msg))
        if not isinstance(parsed, dict):
            raise ValueError("Extra fields must be one JSON object.")
        return json.dumps(parsed, indent=2, ensure_ascii=False)

    @model_validator(mode="after")
    def disabled_sources_need_reason(self):
        if not self.enabled and not self.disabled_reason:
            raise ValueError("Add a reason when you disable a source.")
        return self
