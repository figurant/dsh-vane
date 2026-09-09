import os
import pytest
from conftest import run


@pytest.mark.external
@pytest.mark.skipif(not os.environ.get("DSH_VANE_TEST_PG_DSN"), reason="DSH_VANE_TEST_PG_DSN is not configured")
def test_real_postgresql_readonly(runtime_factory):
    r = runtime_factory(databases=[{"alias": "test", "kind": "postgresql", "dsnEnv": "DSH_VANE_TEST_PG_DSN"}])
    result = run(r, "load", {"source": {"kind": "database", "source_alias": "test", "query": "SELECT %s::integer value", "bindings": [42]}})
    assert result["tables"][0]["preview"][0]["value"] == 42
    with pytest.raises(Exception):
        run(r, "load", {"source": {"kind": "database", "source_alias": "test", "query": "CREATE TABLE forbidden_by_readonly(n int)"}})
