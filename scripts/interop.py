"""Generate/independently consume the shared protocol golden fixture."""
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import jsonschema
import pyarrow.parquet as pq

from dsh_vane_runtime.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def consume(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text())
    jsonschema.Draft202012Validator(json.loads((ROOT / "contracts/artifact-v1.schema.json").read_text())).validate(manifest)
    for entry in manifest["sources"] + manifest["outputs"]:
        path = (folder / entry["path"]).resolve()
        assert path.is_relative_to(folder) and not Path(entry["path"]).is_absolute()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    original = (folder / manifest["sources"][0]["path"]).read_bytes()
    assert original == b"item,value\nA,10\nB,20\n"
    facts = next(o for o in manifest["outputs"] if o["name"] == "facts")
    evidence = next(o for o in manifest["outputs"] if o["name"] == "evidence")
    rows = pq.read_table(folder / facts["path"]).to_pylist()
    locations = pq.read_table(folder / evidence["path"]).to_pylist()
    assert len(rows) == 2 and sum(r["value"] for r in rows) == 30
    assert sorted(json.loads(r["locator_json"])["row"] for r in locations) == [2, 3]
    return {"status": "passed", "artifact_id": manifest["artifact_id"], "input_sha256": hashlib.sha256(original).hexdigest(), "facts_rows": len(rows), "value_sum": 30, "evidence_rows": [2, 3], "reader": "independent JSON Schema + hashlib + PyArrow", "package_digest": manifest["package"]["digest"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--consume", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "tests/fixtures/golden")
    args = parser.parse_args()
    if args.consume:
        print(json.dumps(consume(args.consume), indent=2))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vane-golden-") as tmp:
        temp = Path(tmp)
        source = temp / "input.csv"
        source.write_bytes(b"item,value\nA,10\nB,20\n")
        config = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", "import {resolveConfig} from './dist/index.js';console.log(JSON.stringify(resolveConfig()))"], cwd=ROOT))
        config["allowedFileRoots"] = [str(temp)]
        workspace = temp / "workspace"
        workspace.mkdir()
        runtime = Runtime({"config": config, "workspace_root": str(workspace), "scope_id": "golden-v1"}, str(uuid4()))
        def run(action, data):
            return runtime.run(action, data, str(uuid4()), lambda: None, threading.RLock())
        try:
            loaded = run("load", {"source": {"kind": "file", "path": str(source)}})
            data = run("execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]]})
            produced = Path(data["artifacts"][0]["manifest"]).parent
            dest = args.output / produced.name
            shutil.copytree(produced, dest)
            receipt = consume(dest)
            receipt["artifact_path"] = str(dest.relative_to(args.output))
            (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt, indent=2))
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
