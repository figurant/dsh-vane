import json
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from dsh_vane_runtime.runtime import Runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime_factory(tmp_path):
    config = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", "import {resolveConfig} from './dist/index.js';console.log(JSON.stringify(resolveConfig()))"], cwd=ROOT))
    config["workspaceRoot"] = str(tmp_path)
    config["allowedFileRoots"] = [str(tmp_path), str(ROOT / "tests/fixtures")]
    instances = []
    def create(**overrides):
        current = {**config, **overrides}
        workspace_id = str(uuid4())
        root = tmp_path / "test-scope" / workspace_id
        root.mkdir(parents=True)
        instance = Runtime({"workspace_root": str(root), "scope_id": "test-scope", "config": current}, workspace_id)
        instances.append(instance)
        return instance
    yield create
    for instance in instances:
        instance.close()


def run(runtime, action, args):
    return runtime.run(action, args, str(uuid4()), lambda: None, threading.RLock())
