from benchmarks.sustained import SyntheticCursor
from src.serving.online_features import entity_keys
from src.serving.schema import TransactionEvent


def test_sustained_fixture_covers_drift_fields_and_preserves_hot_card() -> None:
    cursor = SyntheticCursor(first_id=101000000, cards=1)
    first, second = (TransactionEvent.model_validate(cursor.take()) for _ in range(2))
    assert first.TransactionID + 1 == second.TransactionID
    assert first.card2 is not None
    assert first.P_emaildomain and first.R_emaildomain
    assert first.DeviceType and first.DeviceInfo and first.has_identity
    assert entity_keys(first.to_record()) == entity_keys(second.to_record())
