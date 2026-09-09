"""Verify execution using an installed wheel rather than editable project source."""
import json
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import dsh_vane_runtime
from dsh_vane_runtime.common import schema_path
from dsh_vane_runtime.runtime import Runtime

root = Path(__file__).resolve().parents[1]
installed = Path(dsh_vane_runtime.__file__).resolve()
assert not installed.is_relative_to(root / "python"), "Expected an isolated wheel installation"
assert schema_path("artifact-v1.schema.json").is_relative_to(installed.parent)
assert (installed.parent / "uv.lock").is_file()
config = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", "import {resolveConfig} from './dist/index.js';console.log(JSON.stringify(resolveConfig()))"], cwd=root))
with tempfile.TemporaryDirectory(prefix="vane-wheel-") as temporary:
    runtime = Runtime({"config": config, "scope_id": "wheel-test", "workspace_root": temporary}, str(uuid4()))
    try:
        runtime.connection.execute("CREATE TABLE persisted AS SELECT 42 n")
        assert runtime.connection.execute("SELECT n FROM persisted").fetchone()[0] == 42
        assert runtime.packages["document_observations"].id == "document_observations"
        print(json.dumps({"status": "passed", "wheel_runtime_version": dsh_vane_runtime.__version__, "editable_source_imported": False, "schemas_and_lock_bundled": True, "real_vane": 42}))
    finally:
        runtime.close()
