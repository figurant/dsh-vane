import json
import shutil
from pathlib import Path
from uuid import uuid4

from .common import RuntimeFailure, now, relative_file, schema_path, sha, uuid_value, validate_schema, write_json

FORMATS = {"parquet", "jsonl", "markdown", "png", "jpeg", "webp"}


def verify_artifact(folder, scope):
    folder = Path(folder)
    manifest = json.loads(relative_file(folder, "manifest.json").read_text())
    validate_schema(manifest, schema_path("artifact-v1.schema.json"))
    uuid_value(manifest["artifact_id"])
    if manifest["artifact_id"] != folder.name or manifest["scope_id"] != scope:
        raise RuntimeFailure("ARTIFACT_SCOPE_MISMATCH", "Artifact ID or authorized scope does not match.")
    names = [o["name"] for o in manifest["outputs"]]
    if len(set(names)) != len(names):
        raise RuntimeFailure("CONTRACT_INVALID", "Artifact output names must be unique.")
    for entry in [*manifest["sources"], *manifest["outputs"]]:
        if "format" in entry and entry["format"] not in FORMATS:
            raise RuntimeFailure("ARTIFACT_FORMAT_UNSUPPORTED", "Artifact uses an unsupported format.")
        if sha(relative_file(folder, entry["path"])) != entry["sha256"]:
            raise RuntimeFailure("ARTIFACT_SHA_MISMATCH", "Artifact file SHA-256 does not match its manifest.")
    return manifest


class ArtifactStore:
    def __init__(self, root, scope):
        self.root, self.scope = Path(root), scope
        self.root.mkdir(parents=True, exist_ok=True)

    def cached(self, recipe):
        for manifest in self.root.glob("*/manifest.json"):
            if manifest.parent.name.startswith("."):
                continue
            value = json.loads(manifest.read_text())
            if value.get("recipe_hash") == recipe:
                return verify_artifact(manifest.parent, self.scope)
        return None

    def stage(self, recipe, package, sources, outputs, recipe_details):
        artifact_id = str(uuid4())
        stage = self.root / (".pending-" + artifact_id)
        stage.mkdir()
        try:
            copied_sources, copied_outputs = [], []
            for i, source in enumerate(sources):
                dest = stage / "sources" / (str(i) + Path(source["path"]).suffix)
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(source["path"], dest)
                if sha(dest) != source["sha256"]:
                    raise RuntimeFailure("SOURCE_SHA_MISMATCH", "Source changed before artifact publication.")
                copied_sources.append({**source, "path": dest.relative_to(stage).as_posix()})
            for i, output in enumerate(outputs):
                dest = stage / "outputs" / (str(i) + Path(output["path"]).suffix)
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(output["path"], dest)
                copied_outputs.append({**output, "path": dest.relative_to(stage).as_posix(), "sha256": sha(dest)})
            value = {"schema_version": "vane-artifact/v1", "artifact_id": artifact_id, "scope_id": self.scope,
                     "created_at": now(), "recipe_hash": recipe, "recipe": recipe_details, "package": package,
                     "sources": copied_sources, "outputs": copied_outputs}
            validate_schema(value, schema_path("artifact-v1.schema.json"))
            for item in [*copied_sources, *copied_outputs]:
                if sha(relative_file(stage, item["path"])) != item["sha256"]:
                    raise RuntimeFailure("ARTIFACT_SHA_MISMATCH", "Staged artifact verification failed.")
            write_json(stage / "manifest.json", value)
            return stage, value
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def publish(self, stage, manifest):
        dest = self.root / manifest["artifact_id"]
        Path(stage).rename(dest)
        return {"artifact_id": manifest["artifact_id"], "manifest": str(dest / "manifest.json"), "recipe_hash": manifest["recipe_hash"]}
