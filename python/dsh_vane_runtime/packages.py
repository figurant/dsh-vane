import importlib
import sys
import types
from pathlib import Path

import yaml

from .common import RuntimeFailure, digest, relative_file, schema_path, sha, validate_schema


def package_digest(root):
    root = Path(root).resolve()
    manifest = yaml.safe_load(relative_file(root, "package.yaml").read_text())
    validate_schema(manifest, schema_path("package-v1.schema.json"))
    paths = ["package.yaml", *manifest["files"]]
    if len(paths) != len(set(paths)):
        raise RuntimeFailure("PACKAGE_INVALID", "Package files must be unique; package.yaml is included automatically.")
    entries = [{"path": p, "sha256": sha(relative_file(root, p))} for p in sorted(paths)]
    return digest(entries), manifest


class Package:
    def __init__(self, config):
        self.root = Path(config["path"]).resolve()
        self.digest, self.manifest = package_digest(self.root)
        if self.digest != config["digest"] or self.manifest["id"] != config["id"] or self.manifest["version"] != config["version"]:
            raise RuntimeFailure("PACKAGE_DIGEST_MISMATCH", "Package ID/version/SHA does not match the host allowlist.")
        self.id, self.version = config["id"], config["version"]
        self.namespace = "_dsh_vane_pkg_" + self.digest
        module = types.ModuleType(self.namespace)
        module.__path__ = [str(self.root)]
        sys.modules[self.namespace] = module
        self.functions = []
        for udf in self.manifest.get("udfs", []):
            module_name, fn_name = udf["entry"].split(":")
            self.file(module_name.replace(".", "/") + ".py")
            module = importlib.import_module(self.namespace + "." + module_name)
            # Vane's local UDF worker is another interpreter. The isolated package
            # namespace is synthetic, so serialize its code rather than an import by name.
            import cloudpickle
            cloudpickle.register_pickle_by_value(module)
            function = getattr(module, fn_name)
            if not callable(function):
                raise RuntimeFailure("PACKAGE_INVALID", "Declared UDF is not callable.")
            self.functions.append((udf, function))
        # Package code cannot hide undeclared import/dependency/SQL/Prompt files.
        declared = set(self.manifest["files"]) | {"package.yaml", "digest.sha256"}
        for path in self.root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.relative_to(self.root).as_posix() not in declared:
                raise RuntimeFailure("PACKAGE_INVALID", "All package files must be declared in files.")
        for pipeline in self.manifest["pipelines"].values():
            for path in pipeline["steps"]:
                self.file(path)
            if "parameters_schema" in pipeline:
                self.file(pipeline["parameters_schema"])
        # Dependencies are installed by the explicit locked setup command, then
        # verified here before any package code can run a pipeline.
        from importlib.metadata import PackageNotFoundError, version
        from packaging.requirements import Requirement
        for filename in self.manifest["files"]:
            if filename.endswith("requirements.lock"):
                for line in self.file(filename).read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    requirement = Requirement(line)
                    if requirement.marker and not requirement.marker.evaluate():
                        continue
                    try:
                        installed = version(requirement.name)
                    except PackageNotFoundError:
                        raise RuntimeFailure("PACKAGE_DEPENDENCY_MISSING", "Run the locked runtime setup to install package dependencies.") from None
                    if installed not in requirement.specifier:
                        raise RuntimeFailure("PACKAGE_DEPENDENCY_MISMATCH", "Installed dependency does not satisfy the package lock.")

    def file(self, path):
        if path not in self.manifest["files"]:
            raise RuntimeFailure("PACKAGE_INVALID", "Referenced package file is not in the files manifest.")
        return relative_file(self.root, path)

    def verify(self):
        if package_digest(self.root)[0] != self.digest:
            raise RuntimeFailure("PACKAGE_DIGEST_MISMATCH", "Package changed after registration.")

    def describe(self):
        import json
        return {"id": self.id, "version": self.version, "digest": self.digest,
                "udfs": self.manifest.get("udfs", []),
                "pipelines": {name: {"parameters_schema": json.loads(self.file(p["parameters_schema"]).read_text()) if "parameters_schema" in p else {"type": "object", "additionalProperties": False}, "outputs": p["outputs"]} for name, p in self.manifest["pipelines"].items()}}
