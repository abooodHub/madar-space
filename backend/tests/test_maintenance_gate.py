from __future__ import annotations

import pytest

from xsa.maintenance_gate import arm_maintenance, maintenance_active, release_maintenance


def test_maintenance_gate_is_exclusive_and_release_is_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    assert not maintenance_active(data_dir)
    lock = arm_maintenance(data_dir)
    assert lock["lock_id"]
    assert maintenance_active(data_dir)
    with pytest.raises(FileExistsError):
        arm_maintenance(data_dir)
    assert release_maintenance(data_dir) is True
    assert release_maintenance(data_dir) is False
    assert not maintenance_active(data_dir)
