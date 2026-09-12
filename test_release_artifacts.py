import json
import sqlite3
import zipfile

import pytest
from tools.release_artifacts import snapshot, verify, backup, test_counts as read_test_counts


def test_snapshot_includes_current_new_sources_excludes_data_and_detects_changes(tmp_path):
    root, output = tmp_path / "source", tmp_path / "release"
    root.mkdir(); output.mkdir()
    (root / "new.py").write_text("value = 2\n", encoding="utf-8")
    (root / "data").mkdir(); (root / "data/private.json").write_text('{"private":true}')
    manifest = snapshot(root, output)
    with zipfile.ZipFile(output / "SOURCE.zip") as archive:
        assert archive.namelist() == ["new.py"]
        assert archive.read("new.py") == (root / "new.py").read_bytes()
    verify(root, manifest)
    (root / "new.py").write_text("changed")
    with pytest.raises(ValueError, match="mismatch"):
        verify(root, manifest)
    with pytest.raises(FileExistsError):
        snapshot(root, output)


def test_snapshot_preserves_public_license_and_standalone_schema(tmp_path):
    root, output = tmp_path / "source", tmp_path / "release"
    root.mkdir(); output.mkdir()
    for name in ("LICENSE", "NOTICE"):
        (root / name).write_text("Synthetic license fixture", encoding="utf-8")
    (root / "schema").mkdir()
    (root / "schema/example.json").write_text("{}", encoding="utf-8")
    manifest = snapshot(root, output)
    assert {"LICENSE", "NOTICE", "schema/example.json"} <= manifest.keys()


def test_backup_retains_wal_content_does_not_overwrite_or_migrate(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "backup.db"
    db = sqlite3.connect(source)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE original(value)")
    db.execute("INSERT INTO original VALUES (42)"); db.commit()
    report = backup(source, target)
    assert report["integrity"] == "ok"
    with sqlite3.connect(target) as check:
        assert check.execute("SELECT * FROM original").fetchall() == [(42,)]
    with pytest.raises(FileExistsError):
        backup(source, target)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("original",)]
    db.close()


def test_counts_are_executed_cases_including_parameterization(tmp_path):
    xml, front = tmp_path / "py.xml", tmp_path / "front.json"
    xml.write_text('<testsuites><testsuite><testcase name="a[1]"/><testcase name="a[2]"/><testcase name="b"><skipped/></testcase></testsuite></testsuites>')
    front.write_text(json.dumps({"success": True, "numFailedTests": 0, "numPassedTests": 4}))
    assert read_test_counts(xml, front) == {"pythonPassed": 2, "pythonSkipped": 1, "frontendPassed": 4}
    xml.write_text('<testsuites><testsuite><testcase><failure/></testcase></testsuite></testsuites>')
    with pytest.raises(ValueError):
        read_test_counts(xml, front)


def test_manifest_rejects_escape_path(tmp_path):
    with pytest.raises(ValueError):
        verify(tmp_path, {"../outside": "hash"})
