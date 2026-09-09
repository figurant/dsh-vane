"""Real model extraction check, explicitly separate from autonomous research."""
import json
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from dsh_vane_runtime.common import RuntimeFailure
from dsh_vane_runtime.runtime import Runtime

root = Path(__file__).resolve().parents[1]
out = root / "receipts/vision"
out.mkdir(parents=True, exist_ok=True)
config = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", "import {resolveConfig} from './dist/index.js';console.log(JSON.stringify(resolveConfig()))"], cwd=root))
config["allowedFileRoots"] = [str(root / "examples/materials")]
config["models"] = [{"alias": "local-vision", "baseURL": "http://127.0.0.1:8001/v1", "model": "Qwen2.5-VL-3B-Instruct", "temperature": 0, "maxTokens": 1000}]
workspace = out / "workspaces" / str(uuid4())
workspace.mkdir(parents=True)
r = Runtime({"config": config, "workspace_root": str(workspace), "scope_id": "vision-integration"}, str(uuid4()))
def run(action, data):
    return r.run(action, data, str(uuid4()), lambda: None, threading.RLock())
receipt = {"kind": "real-vision-extraction", "autonomous_research": False}
try:
    loaded = run("load", {"source": {"kind": "file", "path": str(root / "examples/materials/new-report.png")}})
    result = run("execute", {"mode": "pipeline", "package_id": "document_observations", "pipeline": "ingest", "asset_ids": [loaded["assets"][0]["asset_id"]], "params": {"model_alias": "local-vision"}})
    receipt["result"] = result
    facts = next(t for t in result["tables"] if t["name"].endswith(".facts"))["preview"]
    receipt["expected_observations"] = [{"item": "A", "value": 15}, {"item": "B", "value": 25}]
    receipt["status"] = "passed" if sorted((f["item"], f["value"]) for f in facts) == [("A", 15), ("B", 25)] else "model_quality_failed"
    extraction = r.connection.execute("SELECT payload FROM " + result["schema"] + ".extracted").fetchall()
    (out / "model-extraction.json").write_text(json.dumps([json.loads(p[0]) for p in extraction], ensure_ascii=False, indent=2))
except RuntimeFailure as e:
    receipt.update(status="failed", error={"code": e.code, "message": str(e)})
finally:
    r.close()
(out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: v for k, v in receipt.items() if k != "result"}, ensure_ascii=False, indent=2))
