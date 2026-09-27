import pytest

from ripple.model import BUCKETS, Edge, bucket_quantity, bucket_range, demand_flow
from tests.conftest import edge


def test_edge_id_is_derived_from_key() -> None:
    e = Edge.model_validate(edge("company/asml", "PRODUCES", "product/euv-lithography"))
    # First 10 hex characters of sha256("company/asml|PRODUCES|product/euv-lithography").
    assert e.id == "e-b84ff709f5"
    assert e.key == ("company/asml", "PRODUCES", "product/euv-lithography")


def test_demand_flow_direction() -> None:
    def flow(src: str, rel: str, dst: str) -> tuple[str, str] | None:
        return demand_flow(Edge.model_validate(edge(src, rel, dst)))

    assert flow("theme/t", "DRIVES", "product/p") == ("theme/t", "product/p")
    assert flow("product/p", "REQUIRES", "product/q") == ("product/p", "product/q")
    assert flow("company/c", "PRODUCES", "product/p") == ("product/p", "company/c")
    assert flow("company/s", "SUPPLIES", "company/c") == ("company/c", "company/s")
    assert flow("company/s", "SUBSIDIARY_OF", "company/p") == ("company/s", "company/p")
    assert flow("product/p", "SUBSTITUTES", "product/q") is None
    assert flow("company/a", "COMPETES_WITH", "company/b") is None
    assert flow("company/a", "EXPOSED_TO", "region/tw") is None  # stored, not traversed (D43)


def test_bucket_tables() -> None:
    assert BUCKETS["revenue"] == {"minor": 0.05, "meaningful": 0.25, "core": 0.6, "pure": 0.9}
    assert BUCKETS["demand"] == {"minor": 0.1, "major": 0.4, "dominant": 0.8}
    assert bucket_quantity("PRODUCES") == "revenue"
    assert bucket_quantity("SUPPLIES") == "revenue"
    assert bucket_quantity("REQUIRES") == "demand"
    assert bucket_quantity("DRIVES") == "demand"
    assert bucket_quantity("SUBSTITUTES") is None


def test_bucket_range_uses_geometric_midpoints() -> None:
    low, high = bucket_range("revenue", 0.25)
    assert low == pytest.approx((0.05 * 0.25) ** 0.5)
    assert high == pytest.approx((0.25 * 0.6) ** 0.5)
    # The top bucket is capped at a share of 1.
    assert bucket_range("demand", 0.8)[1] == 1.0
    assert bucket_range("revenue", 0.9)[1] == 1.0
    # The bottom bucket mirrors its upper gap in log space.
    low, high = bucket_range("revenue", 0.05)
    assert low == pytest.approx(0.05 * 0.05 / high)
    assert bucket_range("revenue", 0.3) is None
