from __future__ import annotations

import json
from datetime import datetime

from xsa.paths import SpacePaths


def test_find_spaces_resolves_canonical_and_sidecar_paths_once(tmp_path, monkeypatch):
    import xsa.paths as paths_module

    canonical = tmp_path / "spaces" / "2026" / "10" / "09" / "101010 - Title - @host - canonical123"
    canonical.mkdir(parents=True)
    legacy = tmp_path / "archive" / "legacy-folder"
    legacy.mkdir(parents=True)
    (legacy / "space.json").write_text(json.dumps({"space_id": "legacy456"}), encoding="utf-8")

    original_walk = paths_module.os.walk
    walks = 0

    def counted_walk(*args, **kwargs):
        nonlocal walks
        walks += 1
        yield from original_walk(*args, **kwargs)

    monkeypatch.setattr(paths_module.os, "walk", counted_walk)

    resolved = SpacePaths.find_spaces(tmp_path, ["canonical123", "legacy456", "missing789"])

    assert resolved["canonical123"].root == canonical
    assert resolved["legacy456"].root == legacy
    assert "missing789" not in resolved
    assert walks == 1


def test_planned_space_folder_gets_verified_title_without_creating_join_artifacts(tmp_path):
    planned = SpacePaths.planned_path(
        tmp_path,
        "space123",
        datetime(2026, 10, 9, 10, 11, 12),
        title="عنوان المساحة غير المسجل",
        host="@host",
    )

    titled = planned.update_title("عنوان حقيقي")

    assert titled.root.name == "131112 - عنوان حقيقي - @host - space123"
    assert not titled.root.exists()
    assert not planned.root.exists()
