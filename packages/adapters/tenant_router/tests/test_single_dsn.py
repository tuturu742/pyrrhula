from adapters.tenant_router.single_dsn import SingleDsnTenantRouter
from core.ports.tenant_router import TenantRouter
from core.tenancy.models import Tenant


def test_returns_same_dsn_regardless_of_tenant_fields() -> None:
    router: TenantRouter = SingleDsnTenantRouter(dsn="postgresql://x")

    tenant_a = Tenant(slug="a", name="A", region="eu", isolation_mode="shared")
    tenant_b = Tenant(slug="b", name="B", region="us", isolation_mode="schema")

    assert router.dsn_for(tenant_a) == "postgresql://x"
    assert router.dsn_for(tenant_b) == "postgresql://x"
