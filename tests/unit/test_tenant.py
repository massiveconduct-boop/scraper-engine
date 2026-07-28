# tests/unit/test_tenant.py
"""TenantId validation — spec §2 core/tenant.py."""

import pytest

from scraper_engine.core.tenant import TenantId


class TestTenantId:
    def test_valid_tenant_ids(self) -> None:
        assert str(TenantId("mystore")) == "mystore"
        assert str(TenantId("abc123")) == "abc123"
        assert str(TenantId("a_b_c")) == "a_b_c"
        assert str(TenantId("a" * 63) == "a" * 63)  # max length

    def test_invalid_too_short(self) -> None:
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("ab")  # need 3+ chars

    def test_invalid_too_long(self) -> None:
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("a" * 64)  # max 63 chars

    def test_invalid_start_char(self) -> None:
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("0abc")  # must start with letter
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("_abc")  # must start with letter

    def test_invalid_chars(self) -> None:
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("abc def")  # spaces
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("abc-def")  # hyphens
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("abc.def")  # dots

    def test_sql_injection_blocked(self) -> None:
        """TenantId must reject SQL-injection-shaped input (spec F-10, F-11, F-31)."""
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("foo; drop schema public")
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("foo'--")
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId('foo" OR 1=1')
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId("foo; DROP TABLE")

    def test_not_string(self) -> None:
        with pytest.raises(ValueError, match="invalid tenant_id"):
            TenantId(123)  # type: ignore[arg-type]

    def test_equality(self) -> None:
        t1 = TenantId("testtenant")
        t2 = TenantId("testtenant")
        t3 = TenantId("othertenant")
        assert t1 == t2
        assert t1 != t3
        assert hash(t1) == hash(t2)

    def test_repr(self) -> None:
        t = TenantId("testtenant")
        assert "testtenant" in repr(t)

    def test_pydantic_validation(self) -> None:
        """Test TenantId works as a Pydantic field type."""
        from pydantic import BaseModel, ValidationError

        class Model(BaseModel):
            tenant: TenantId

        m = Model(tenant="validname")  # type: ignore[arg-type]
        assert isinstance(m.tenant, TenantId)
        with pytest.raises(ValidationError):
            Model(tenant="bad name")  # type: ignore[arg-type]
