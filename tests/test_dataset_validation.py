from __future__ import annotations

from app.dataset_validation import validate_dataset


def test_dataset_validation_passes_for_current_data() -> None:
    assert validate_dataset("data") == []
