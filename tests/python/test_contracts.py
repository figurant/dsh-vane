import hashlib
import json
import shutil
from pathlib import Path

import pytest

from dsh_vane_runtime.artifacts import verify_artifact
from dsh_vane_runtime.common import RuntimeFailure, canonical, relative_file, sha
from dsh_vane_runtime.packages import package_digest
from conftest import ROOT, run


def test_golden_package_and_artifact_independent_reader(runtime_factory):
    import pyarrow.parquet as pq
    r = runtime_factory()
    source = ROOT / "tests/fixtures/input.csv"
    assert source.read_bytes() == b"item,value\nA,10\nB,20\n"
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(source)}})
    result = run(r, "execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]})
    folder = Path(result["artifacts"][0]["manifest"]).parent
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["sources"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    # Independent reader uses only the serialized contract and Arrow, not the producer's loader.
    for item in manifest["sources"] + manifest["outputs"]:
        assert hashlib.sha256((folder / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    facts = next(o for o in manifest["outputs"] if o["name"] == "facts")
    rows = pq.read_table(folder / facts["path"]).to_pylist()
    assert len(rows) == 2 and sum(r["value"] for r in rows) == 30
    evidence = next(o for o in manifest["outputs"] if o["name"] == "evidence")
    assert [json.loads(r["locator_json"])["row"] for r in pq.read_table(folder / evidence["path"]).to_pylist()] == [2, 3]
    assert verify_artifact(folder, "test-scope") == manifest
    with pytest.raises(RuntimeFailure, match="scope"):
        verify_artifact(folder, "other-scope")
    (folder / facts["path"]).write_bytes(b"corrupted")
    with pytest.raises(RuntimeFailure) as error:
        verify_artifact(folder, "test-scope")
    assert error.value.code == "ARTIFACT_SHA_MISMATCH"


def test_digest_canonicalization_and_traversal(tmp_path):
    source = ROOT / "packages/document_observations"
    computed, manifest = package_digest(source)
    entries = [{"path": p, "sha256": hashlib.sha256((source / p).read_bytes()).hexdigest()} for p in sorted(["package.yaml", *manifest["files"]])]
    assert computed == hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert computed == (source / "digest.sha256").read_text().strip()
    with pytest.raises(RuntimeFailure):
        relative_file(source, "../package.yaml")
    (tmp_path / "outside").symlink_to("/etc/passwd")
    with pytest.raises(RuntimeFailure):
        relative_file(tmp_path, "outside")


def test_pipeline_invalid_params_and_failure_publish_no_manifest(runtime_factory):
    r = runtime_factory()
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(ROOT / "tests/fixtures/input.csv")}})
    with pytest.raises(RuntimeFailure) as failure:
        run(r, "execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]], "params": {"undeclared": True}})
    assert failure.value.code == "INVALID_ARGUMENT"
    assert list((r.root / "artifacts").glob("*/manifest.json")) == []
    assert r.connection.execute("SELECT count(*) FROM main.input_files").fetchone()[0] == 1


def test_checkpoint_tamper_and_restore_input_paths(runtime_factory):
    r = runtime_factory()
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(ROOT / "tests/fixtures/input.csv")}, "table_name": "history"})
    cp = run(r, "checkpoint", {"tables": ["main.history", "main.input_files"]})
    restored = runtime_factory()
    data = run(restored, "restore", {"checkpoint_id": cp["checkpoint_id"]})
    assert restored.connection.execute("SELECT sum(value) FROM history").fetchone()[0] == 30
    assert all(Path(a["path"]).is_relative_to(restored.root) for a in data["assets"])
    folder = r.root.parent / "checkpoints" / cp["checkpoint_id"]
    manifest = json.loads((folder / "manifest.json").read_text())
    (folder / manifest["tables"][0]["path"]).write_bytes(b"bad")
    with pytest.raises(RuntimeFailure) as error:
        run(runtime_factory(), "restore", {"checkpoint_id": cp["checkpoint_id"]})
    assert error.value.code == "CHECKPOINT_SHA_MISMATCH"


def test_parquet_and_pdf_load_and_extract(runtime_factory, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pymupdf
    r = runtime_factory()
    path = tmp_path / "data.parquet"
    pq.write_table(pa.table({"item": ["A", "B"], "value": [10, 20]}), path)
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(path)}})
    assert loaded["tables"][0]["row_count"] == 2
    pdf = tmp_path / "new-report.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "Operational report\nA,15\nB,25")
        doc.save(pdf)
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(pdf)}})
    assert loaded["tables"] == []
    result = run(r, "execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]})
    facts = next(t for t in result["tables"] if t["name"].endswith(".facts"))
    assert sum(row["value"] for row in facts["preview"]) == 40
    evidence = next(t for t in result["tables"] if t["name"].endswith(".evidence"))
    assert json.loads(evidence["preview"][0]["locator_json"]) == {"page": 1}


def test_image_without_model_is_explicit_failure(runtime_factory, tmp_path):
    from PIL import Image
    r = runtime_factory()
    path = tmp_path / "record.png"
    Image.new("RGB", (64, 64), "white").save(path)
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(path)}})
    with pytest.raises(RuntimeFailure) as error:
        run(r, "execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]})
    assert error.value.code == "MODEL_ERROR"
    assert not list((r.root / "artifacts").glob("*/manifest.json"))


def test_cancellation_at_publication_rolls_back_and_never_publishes(runtime_factory):
    import threading
    from uuid import uuid4
    r = runtime_factory()
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(ROOT / "tests/fixtures/input.csv")}})
    def cancel_at_publish():
        if r.pending_artifacts:
            raise RuntimeFailure("CANCELLED", "test cancellation at publication barrier")
    with pytest.raises(RuntimeFailure) as error:
        r.run("execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]}, str(uuid4()), cancel_at_publish, threading.RLock())
    assert error.value.code == "CANCELLED"
    assert not list((r.root / "artifacts").glob("*/manifest.json"))
    assert not r.connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema LIKE 'run_%'").fetchall()


def test_artifact_unknown_version_format_and_paths(runtime_factory, tmp_path):
    r = runtime_factory()
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(ROOT / "tests/fixtures/input.csv")}})
    result = run(r, "execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]})
    folder = Path(result["artifacts"][0]["manifest"]).parent
    path = folder / "manifest.json"
    original = json.loads(path.read_text())
    mutations = [("version", "CONTRACT_INVALID"), ("format", "ARTIFACT_FORMAT_UNSUPPORTED"), ("path", "INVALID_PATH"), ("missing", "CONTRACT_INVALID")]
    for mutation, code in mutations:
        changed = json.loads(json.dumps(original))
        if mutation == "version":
            changed["schema_version"] = "vane-artifact/v2"
        elif mutation == "format":
            changed["outputs"][0]["format"] = "executable"
        elif mutation == "path":
            changed["sources"][0]["path"] = "../../outside"
        else:
            del changed["package"]
        path.write_text(json.dumps(changed))
        with pytest.raises(RuntimeFailure) as error:
            verify_artifact(folder, "test-scope")
        assert error.value.code == code
    path.write_text(json.dumps(original))


def test_images_publish_bytes_and_import_rewrites_producer_paths(runtime_factory, tmp_path):
    import yaml
    from PIL import Image
    package = tmp_path / "image-package"
    package.mkdir()
    manifest = {"schema_version": "vane-package/v1", "id": "image_example", "version": "1.0.0", "files": ["images.sql"], "pipelines": {"ingest": {"steps": ["images.sql"], "outputs": {"images": "images"}}}}
    (package / "package.yaml").write_text(yaml.safe_dump(manifest))
    (package / "images.sql").write_text("CREATE TABLE images AS SELECT 'chart.png'::VARCHAR original_ref, path::VARCHAR path, media_type::VARCHAR mime_type FROM input_files;")
    pin = {"id": "image_example", "version": "1.0.0", "path": str(package), "digest": package_digest(package)[0]}
    r = runtime_factory(enabledPackages=[pin])
    path = tmp_path / "image.png"
    Image.new("RGB", (8, 8), "white").save(path)
    loaded = run(r, "load", {"source": {"kind": "file", "path": str(path)}})
    result = run(r, "execute", {"mode": "pipeline", "package_id": "image_example", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]})
    imported = runtime_factory(enabledPackages=[pin])
    value = run(imported, "load", {"source": {"kind": "artifact", "store_alias": "workspace", "artifact_id": result["artifacts"][0]["artifact_id"]}})
    row = value["tables"][0]["preview"][0]
    assert row["original_ref"] == "chart.png"
    assert Path(row["path"]).is_relative_to(imported.root)
    assert Path(row["path"]).read_bytes() == path.read_bytes()
