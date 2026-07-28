# core/tenant.py
import re

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

_TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


class TenantId(str):
    """
    The ONLY type accepted at any storage/proxy/queue boundary.
    Construction is the single validation gate (F-10, F-11, F-31 closure).
    Raises ValueError on anything that isn't a safe SQL-identifier-shaped string.
    """

    def __new__(cls, value: str) -> "TenantId":
        if not isinstance(value, str) or not _TENANT_RE.match(value):
            raise ValueError(f"invalid tenant_id: {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(cls, handler(str))
