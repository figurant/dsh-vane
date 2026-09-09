import copy
import importlib.metadata
import json
import mimetypes
import os
import platform
import shutil
import sysconfig
import tempfile
from pathlib import Path
from uuid import uuid4

import vane

from . import __version__
from .artifacts import ArtifactStore, verify_artifact
from .common import (RuntimeFailure, canonical, contained, digest, identifier, json_value, now,
                     qualified, redact, relative_file, sha, uuid_value, validate_schema, write_json)
from .packages import Package


class PythonContext:
    """Plugin-owned convenience interface, not a Vane API claim."""
    def __init__(self, runtime):
        self._runtime = runtime
        self.connection = runtime.connection

    def register_udf(self, name, fn, parameters, return_type):
        self._runtime.register_udf(name, fn, parameters, return_type, dynamic=True)

    def publish(self, table_name):
        qualified(table_name)
        self._runtime.to_publish.append(table_name)


class Runtime:
    def __init__(self, options, workspace_id):
        self.workspace_id = workspace_id
        self.root = Path(options["workspace_root"]).resolve()
        self.scope = options["scope_id"]
        self.config = options["config"]
        self.limits = self.config["limits"]
        if importlib.metadata.version("vane-ai") != "0.1.0":
            raise RuntimeFailure("VANE_VERSION_UNSUPPORTED", "This runtime supports vane-ai 0.1.0.")
        os.environ["DSH_VANE_MODELS"] = canonical(self.config.get("models", []))
        for name in ("inputs", "results", "staging", "artifacts"):
            (self.root / name).mkdir(exist_ok=True)
        vane.configure(runner="local")
        self.connection = vane.connect(str(self.root / "workspace.vane"))
        self.connection.execute(f"SET memory_limit='{self.limits['maxMemoryMB']}MB'")
        self.connection.execute(f"SET max_temp_directory_size='{self.limits['maxDiskBytes']}B'")
        self.connection.execute("CREATE SCHEMA _vane_results")
        self.connection.execute("CREATE TABLE input_files(asset_id VARCHAR, path VARCHAR, media_type VARCHAR, sha256 VARCHAR, original_name VARCHAR)")
        self.assets, self.results, self.tables, self.udfs = {}, {}, {}, {}
        self.packages = {}
        for config in self.config["enabledPackages"]:
            package = Package(config)
            self.packages[package.id] = package
            for spec, fn in package.functions:
                self.register_udf(spec["name"], fn, spec["parameters"], spec["return_type"])
        self.store = ArtifactStore(self.root / "artifacts", self.scope)
        self.history = []
        self.globals = {"__name__": "__dsh_vane_task__", "ctx": PythonContext(self)}
        self.to_publish, self.pending_artifacts, self.pending_checkpoints = [], [], []
        self.check = lambda: None

    def capabilities(self):
        return {"protocol": "dsh-vane-ipc/v1", "runtime_version": __version__, "vane_version": vane.__version__,
                "host_version": self.config["hostVersion"], "modes": ["sql", "python", "pipeline"],
                "interrupt": "connection.interrupt; hard process-group termination after host grace period",
                "packages": [p.describe() for p in self.packages.values()], "persistent": True,
                "limitations": ["Trusted local Python/SQL, not a sandbox", "Unmaterialized Relations and arbitrary Python globals are not checkpointed"]}

    def environment_versions(self):
        # Ray adds its vendored module directory to sys.path on first UDF use.
        # Inspect installed distributions, not that changing import search path.
        paths = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
        return {"python": platform.python_version(), "dependencies": sorted((d.metadata["Name"].lower(), d.version) for d in importlib.metadata.distributions(path=paths))}

    def register_udf(self, name, fn, parameters, return_type, dynamic=False):
        identifier(name)
        if name in self.udfs:
            raise RuntimeFailure("UDF_ALREADY_EXISTS", "Use a new UDF name to preserve existing computation versions.")
        if not callable(fn) or not isinstance(parameters, list) or not isinstance(return_type, str):
            raise RuntimeFailure("INVALID_ARGUMENT", "Invalid UDF function or signature.")
        try:
            vane.attach_function(vane.func(fn, return_dtype=return_type), connection=self.connection, alias=name, parameters=parameters)
        except Exception:
            raise RuntimeFailure("UDF_REGISTRATION_ERROR", "Vane rejected the UDF signature.") from None
        self.udfs[name] = {"fn": fn, "parameters": parameters, "return_type": return_type, "dynamic": dynamic}

    def interrupt(self):
        self.connection.interrupt()

    def close(self):
        self.connection.close()

    def _disk_check(self):
        total = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        if total > self.limits["maxDiskBytes"]:
            raise RuntimeFailure("LIMIT_EXCEEDED", "Workspace disk quota exceeded; close or export this workspace.")

    def run(self, action, args, operation_id, check, final_lock, completed=lambda: None):
        self.check = check
        self.to_publish, self.pending_artifacts, self.pending_checkpoints = [], [], []
        before = (copy.deepcopy(self.assets), copy.deepcopy(self.results), copy.deepcopy(self.tables), set(self.udfs))
        transaction = action in ("execute", "load", "restore")
        record = {"operation_id": operation_id, "action": action, "started_at": now(),
                  "request": json.loads(redact(json_value(args))), "request_digest": digest(json_value(args)),
                  "input_table_versions": {k: v.get("content_digest") for k, v in self.tables.items()}}
        old_schema = self.connection.execute("SELECT current_schema()").fetchone()[0]
        published = []
        try:
            check()
            self._disk_check()
            if transaction:
                self.connection.execute("BEGIN TRANSACTION")
            method = getattr(self, action, None)
            if action not in {"execute", "load", "describe", "read", "checkpoint", "restore"} or method is None:
                raise RuntimeFailure("INVALID_ARGUMENT", "Unknown runtime action.")
            data = method(args)
            self._disk_check()
            # Finish fallible serialization/catalog writes before the final publish.
            record.update(status="succeeded", finished_at=now(), output_tables=[t["name"] for t in data.get("tables", [])])
            result_id = self.record_result([record], "execution")
            data["execution_result_id"] = result_id
            self.history.append(record)
            for _stage, manifest in self.pending_artifacts:
                data.setdefault("artifacts", []).append({"artifact_id": manifest["artifact_id"], "manifest": str(self.store.root / manifest["artifact_id"] / "manifest.json"), "recipe_hash": manifest["recipe_hash"]})
            bounded = self.bounded(data)
            self.persist_catalog()
            # No operation may publish after cancellation has been accepted by the controller.
            with final_lock:
                check()
                if transaction:
                    self.connection.execute("COMMIT")
                for stage, manifest in self.pending_artifacts:
                    self.store.publish(stage, manifest)
                    published.append(self.store.root / manifest["artifact_id"])
                for stage, dest in self.pending_checkpoints:
                    stage.rename(dest)
                    published.append(dest)
                completed()
            return bounded
        except BaseException as error:
            if transaction:
                try:
                    self.connection.execute("ROLLBACK")
                except Exception:
                    pass
            self.assets, self.results, self.tables, prior_udfs = before
            for name in set(self.udfs) - prior_udfs:
                # UDF registration is not transactional. Discard registrations on a failed call.
                try:
                    self.connection.remove_function(name)
                except Exception:
                    pass
                self.udfs.pop(name, None)
            for stage, _ in [*self.pending_artifacts, *self.pending_checkpoints]:
                shutil.rmtree(stage, ignore_errors=True)
            for folder in published:
                shutil.rmtree(folder, ignore_errors=True)
            if isinstance(error, RuntimeFailure):
                failure = error
            else:
                text = str(error)
                code = "MODEL_ERROR" if "MODEL_" in text else "UDF_ERROR" if "UDF" in text or args.get("mode") == "python" else "SQL_ERROR" if action == "execute" else "SOURCE_ERROR"
                cleaned = redact(text).split("\nStack Trace:", 1)[0]
                if len(cleaned) > 1200:
                    cleaned = cleaned[:350] + "\n…\n" + cleaned[-800:]
                failure = RuntimeFailure(code, cleaned)
            record.update(status="cancelled" if failure.code == "CANCELLED" else "failed", finished_at=now(), error={"code": failure.code, "message": redact(str(failure))})
            if not self.history or self.history[-1] is not record:
                self.history.append(record)
            self.persist_catalog()
            raise failure from None
        finally:
            try:
                self.connection.execute("SET schema = ?", [old_schema])
            except Exception:
                pass
            self.check = lambda: None

    def persist_catalog(self):
        write_json(self.root / "catalog.json", {"assets": self.assets, "tables": self.tables, "results": self.results, "history": self.history})

    def record_result(self, rows, kind="rows"):
        result_id = str(uuid4())
        dest = self.root / "results" / (result_id + ".jsonl")
        with dest.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(canonical(json_value(row)) + "\n")
        self.results[result_id] = {"path": str(dest), "kind": kind, "rows": len(rows)}
        return result_id

    def bounded(self, data):
        if len(canonical(json_value(data)).encode()) <= self.limits["maxResponseBytes"] - 1024:
            return data
        result_id = self.record_result([data], "summary")
        return {"result_id": result_id, "truncated": True, "full_result": self.results[result_id]["path"],
                "warnings": ["Summary exceeded response budget; read the full result."],
                "tables": [{"name": t["name"], "row_count": t["row_count"], "result_id": t["result_id"]} for t in data.get("tables", [])][:20]}

    def read(self, args):
        result_id = args["result_id"]
        if result_id not in self.results:
            raise RuntimeFailure("RESULT_NOT_FOUND", "Result is not in this workspace catalog.")
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise RuntimeFailure("INVALID_ARGUMENT", "Invalid page offset or limit.")
        meta = self.results[result_id]
        rows, used, truncated = [], 0, False
        with contained(self.root, meta["path"]).open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < offset:
                    continue
                if len(rows) >= limit:
                    break
                size = len(line.encode())
                if used + size > self.limits["maxResponseBytes"] - 2048:
                    truncated = True
                    if not rows:
                        rows.append({"_oversized_row": i, "full_result": meta["path"]})
                    break
                rows.append(json.loads(line))
                used += size
        next_offset = offset + len(rows)
        return {"result_id": result_id, "rows": rows, "next_offset": next_offset if next_offset < meta["rows"] else None,
                "truncated": truncated or next_offset < meta["rows"], "total_rows": meta["rows"], "full_result": meta["path"]}

    def table_exists(self, name):
        parts = name.split(".")
        schema, table = parts if len(parts) == 2 else ("main", name)
        return bool(self.connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=?", [schema, table]).fetchone())

    def table_summary(self, name, provenance=None, materialize_result=True):
        q = qualified(name)
        if not self.table_exists(name):
            raise RuntimeFailure("TABLE_NOT_FOUND", "Table does not exist.")
        cursor = self.connection.execute("SELECT * FROM " + q + " LIMIT ?", [self.limits["maxRows"] + 1 if materialize_result else 5])
        schema = [{"name": d[0], "type": str(d[1])} for d in cursor.description]
        columns = [s["name"] for s in schema]
        rows = cursor.fetchall()
        if materialize_result and len(rows) > self.limits["maxRows"]:
            raise RuntimeFailure("LIMIT_EXCEEDED", "Published table exceeds maxRows; aggregate/filter before publishing.")
        records = [dict(zip(columns, json_value(row))) for row in rows]
        previous = self.tables.get(name, {})
        meta = {"name": name, "schema": schema, "row_count": len(rows) if materialize_result else None,
                "preview": records[:5], "provenance": provenance or previous.get("provenance", {"kind": "sql"})}
        if materialize_result:
            meta["result_id"] = self.record_result(records)
            meta["content_digest"] = digest({"schema": schema, "rows": records})
            self.tables[name] = meta
        return meta

    def describe(self, args):
        target = args["target"]
        if target == "functions":
            return {"functions": [{"name": n, "parameters": v["parameters"], "return_type": v["return_type"], "dynamic": v["dynamic"]} for n, v in self.udfs.items()], "packages": [p.describe() for p in self.packages.values()]}
        if target == "table":
            return {"tables": [self.table_summary(args["name"], materialize_result=False)]}
        names = self.connection.execute("SELECT table_schema || '.' || table_name FROM information_schema.tables WHERE table_schema NOT LIKE '_vane%' ORDER BY 1").fetchall()
        return {"tables": [{"name": n[0], "row_count": None, "catalog": self.tables.get(n[0], {})} for n in names],
                "assets": list(self.assets.values()), "packages": [p.describe() for p in self.packages.values()], "operations": self.history[-20:],
                "checkpoints": [p.parent.name for p in (self.root.parent / "checkpoints").glob("*/manifest.json") if not p.parent.name.startswith(".")]}

    def _materialize_cursor(self, provenance):
        # Consume once, then materialize a durable host result table for later pages.
        import pyarrow as pa
        cursor = self.connection
        if cursor.description is None:
            return {"tables": []}
        description = list(cursor.description)
        rows = cursor.fetchmany(self.limits["maxRows"] + 1)
        if len(rows) > self.limits["maxRows"]:
            raise RuntimeFailure("LIMIT_EXCEEDED", "SQL result exceeds maxRows; use SQL aggregation or a bounded SELECT.")
        columns = []
        for i, d in enumerate(description):
            candidate = d[0]
            if candidate in columns:
                candidate += "_" + str(i)
            columns.append(candidate)
        table = "_vane_results.r_" + uuid4().hex
        # Types come exclusively from Vane's own result metadata. Names are quoted as data.
        definitions = ','.join('"' + n.replace('"', '""') + '" ' + str(d[1]) for n, d in zip(columns, description))
        self.connection.execute("CREATE TABLE " + qualified(table) + "(" + definitions + ")")
        if rows:
            transfer = "_transfer_" + uuid4().hex
            try:
                arrow = pa.Table.from_arrays([pa.array([r[i] for r in rows]) for i in range(len(columns))], names=columns)
                self.connection.register(transfer, arrow)
                self.connection.execute("INSERT INTO " + qualified(table) + " SELECT * FROM " + identifier(transfer))
            finally:
                self.connection.unregister(transfer)
        return {"tables": [self.table_summary(table, provenance)]}

    def execute(self, args):
        mode = args["mode"]
        if mode == "sql":
            statements = self.connection.extract_statements(args["sql"])
            if any(str(s.type).split(".")[-1] in {"TRANSACTION", "ATTACH", "DETACH"} for s in statements):
                raise RuntimeFailure("SQL_NOT_ALLOWED", "Host manages transactions and task database attachments.")
            self.connection.execute(args["sql"], args.get("bindings", []))
            return self._materialize_cursor({"kind": "sql", "sql": redact(args["sql"]), "bindings": json.loads(redact(args.get("bindings", []))), "code_digest": digest(args)})
        if mode == "python":
            code = args["code"]
            exec(compile(code, "<vane-task>", "exec"), self.globals)
            self.check()
            return {"tables": [self.table_summary(t, {"kind": "python", "code_digest": digest(code)}) for t in dict.fromkeys(self.to_publish)],
                    "code_digest": digest(code), "warnings": ["Only ctx.publish outputs are catalogued; process globals are not checkpointed."]}
        if mode == "pipeline":
            return self.pipeline(args)
        raise RuntimeFailure("INVALID_ARGUMENT", "Unknown execution mode.")

    def _copy_asset(self, source):
        path = Path(source).resolve()
        allowed = [Path(p).resolve() for p in self.config["allowedFileRoots"]]
        if not any(path.is_relative_to(p) for p in allowed):
            raise RuntimeFailure("PERMISSION_DENIED", "Source is outside allowedFileRoots.")
        if not path.is_file():
            raise RuntimeFailure("FILE_NOT_FOUND", "Source must be an ordinary file.")
        if path.stat().st_size > self.limits["maxFileBytes"]:
            raise RuntimeFailure("LIMIT_EXCEEDED", "Source exceeds maxFileBytes.")
        asset_id = str(uuid4())
        dest = self.root / "inputs" / (asset_id + path.suffix.lower())
        # Resolve/recheck in the runtime as well as the host; O_NOFOLLOW closes the last-component swap.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as src, dest.open("wb") as out:
            if Path("/proc/self/fd").exists():
                actual = Path(os.readlink(f"/proc/self/fd/{src.fileno()}")).resolve()
                if not any(actual.is_relative_to(p) for p in allowed):
                    raise RuntimeFailure("PERMISSION_DENIED", "File changed outside allowed roots while opening.")
            copied = 0
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                self.check()
                copied += len(chunk)
                if copied > self.limits["maxFileBytes"]:
                    raise RuntimeFailure("LIMIT_EXCEEDED", "Source grew beyond maxFileBytes while copying.")
                out.write(chunk)
        media_type = "application/vnd.apache.parquet" if path.suffix.lower() == ".parquet" else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        asset = {"asset_id": asset_id, "path": str(dest), "media_type": media_type, "sha256": sha(dest), "original_name": path.name}
        self.assets[asset_id] = asset
        self.connection.execute("INSERT INTO main.input_files VALUES (?,?,?,?,?)", [asset[k] for k in ("asset_id", "path", "media_type", "sha256", "original_name")])
        return asset

    def load(self, args):
        source = args["source"]
        if source["kind"] == "file":
            asset = self._copy_asset(source["path"])
            data = {"assets": [asset], "tables": [], "provenance": {"kind": "file", "asset_id": asset["asset_id"], "sha256": asset["sha256"]}}
            if asset["media_type"] in ("text/csv", "application/vnd.apache.parquet"):
                name = args.get("table_name", "file_" + uuid4().hex)
                identifier(name)
                reader = "read_csv_auto(?, header=true)" if asset["media_type"] == "text/csv" else "read_parquet(?)"
                self.connection.execute("CREATE TABLE " + identifier(name) + " AS SELECT * FROM " + reader, [asset["path"]])
                data["tables"] = [self.table_summary("main." + name, data["provenance"])]
            else:
                data["warnings"] = ["File bytes registered; select a compatible package/UDF before claiming content was read."]
            return data
        if source["kind"] == "database":
            return self.load_database(source, args.get("table_name"))
        if source["kind"] == "weknora":
            return self.load_knowledge(source)
        if source["kind"] == "artifact":
            return self.load_artifact(source)
        raise RuntimeFailure("SOURCE_UNSUPPORTED", "Unsupported source kind.")

    def load_database(self, source, table_name=None):
        import psycopg
        import pyarrow as pa
        config = next((c for c in self.config["databases"] if c["alias"] == source["source_alias"]), None)
        if config is None:
            raise RuntimeFailure("SOURCE_NOT_FOUND", "Unknown PostgreSQL alias.")
        dsn = os.environ.get(config["dsnEnv"])
        if not dsn:
            raise RuntimeFailure("SOURCE_CONFIG_ERROR", "Configured PostgreSQL DSN environment variable is missing.")
        try:
            with psycopg.connect(dsn, connect_timeout=max(1, self.limits["httpTimeoutMs"] // 1000)) as db:
                db.read_only = True
                with db.transaction():
                    db.execute("SELECT set_config('statement_timeout', %s, true)", [str(self.limits["operationTimeoutMs"])])
                    with db.cursor(name="dsh_vane_read") as cursor:
                        cursor.execute(source["query"], source.get("bindings", []))
                        # A named cursor only accepts a query; SET/COMMIT cannot escape the read-only transaction.
                        rows = cursor.fetchmany(self.limits["maxRows"] + 1)
                        names = [d.name for d in cursor.description]
                        if len(rows) > self.limits["maxRows"]:
                            raise RuntimeFailure("LIMIT_EXCEEDED", "Database query exceeds maxRows.")
                        if len(canonical(json_value(rows)).encode()) > self.limits["maxFileBytes"]:
                            raise RuntimeFailure("LIMIT_EXCEEDED", "Database result exceeds byte quota.")
        except RuntimeFailure:
            raise
        except Exception:
            raise RuntimeFailure("DATABASE_ERROR", "Read-only PostgreSQL query failed; verify bindings, permission and timeout.") from None
        name = table_name or "database_" + uuid4().hex
        identifier(name)
        transfer = "_transfer_" + uuid4().hex
        arrow = pa.Table.from_arrays([pa.array([r[i] for r in rows]) for i in range(len(names))], names=names)
        self.connection.register(transfer, arrow)
        try:
            self.connection.execute("CREATE TABLE " + identifier(name) + " AS SELECT * FROM " + identifier(transfer))
        finally:
            self.connection.unregister(transfer)
        provenance = {"kind": "database", "source_alias": source["source_alias"], "query": redact(source["query"]), "bindings": json.loads(redact(source.get("bindings", []))), "read_only": True}
        return {"tables": [self.table_summary("main." + name, provenance)], "provenance": provenance}

    def load_knowledge(self, source):
        staged = contained(self.root, source["staged_path"])
        knowledge = json.loads(staged.read_text())
        prefix = "knowledge_" + uuid4().hex
        document_table, chunk_table = prefix + "_documents", prefix + "_chunks"
        self.connection.execute(f"CREATE TABLE {identifier(document_table)}(knowledge_id VARCHAR, title VARCHAR, description VARCHAR)")
        doc = knowledge["document"]
        self.connection.execute(f"INSERT INTO {identifier(document_table)} VALUES (?,?,?)", [doc["knowledge_id"], doc["title"], doc["description"]])
        self.connection.execute(f"CREATE TABLE {identifier(chunk_table)}(chunk_id VARCHAR, knowledge_id VARCHAR, chunk_index BIGINT, content VARCHAR)")
        for chunk in knowledge["chunks"]:
            self.connection.execute(f"INSERT INTO {identifier(chunk_table)} VALUES (?,?,?,?)", [chunk[k] for k in ("chunk_id", "knowledge_id", "chunk_index", "content")])
        result = {"tables": [self.table_summary("main." + name, knowledge["provenance"]) for name in (document_table, chunk_table)],
                  "provenance": knowledge["provenance"], "warnings": knowledge["warnings"], "artifact_status": []}
        for artifact_id in knowledge["artifact_ids"]:
            matches = [s for s in self.config["artifactStores"] if (Path(s["root"]) / artifact_id / "manifest.json").is_file()]
            if not matches:
                result["artifact_status"].append({"artifact_id": artifact_id, "status": "unavailable"})
            for store in matches:
                try:
                    loaded = self.load_artifact({"artifact_id": artifact_id, "store_alias": store["alias"], "knowledge_id": source["knowledge_id"], "source_alias": source["source_alias"]})
                    result["tables"].extend(loaded["tables"])
                    result["artifact_status"].append({"artifact_id": artifact_id, "status": "loaded", "store_alias": store["alias"]})
                except RuntimeFailure as e:
                    result["artifact_status"].append({"artifact_id": artifact_id, "status": e.code})
        if not knowledge["artifact_ids"]:
            result["artifact_status"] = [{"status": "no_marker"}]
        return result

    def load_artifact(self, source):
        artifact_id = uuid_value(source["artifact_id"])
        if source["store_alias"] == "workspace":
            matches = list(self.root.parent.glob(f"*/artifacts/{artifact_id}/manifest.json"))
            if not matches:
                raise RuntimeFailure("ARTIFACT_NOT_FOUND", "Artifact is not in this session's owned workspaces.")
            folder, scope = contained(self.root.parent, matches[0].parent), self.scope
        else:
            store = next((s for s in self.config["artifactStores"] if s["alias"] == source["store_alias"]), None)
            if not store or not source.get("knowledge_id") or not source.get("source_alias"):
                raise RuntimeFailure("PERMISSION_DENIED", "Shared artifact requires host-verified knowledge and store alias.")
            folder, scope = contained(store["root"], Path(store["root"]) / artifact_id), store["scope"]
        manifest = verify_artifact(folder, scope)
        schema = "artifact_" + uuid4().hex
        self.connection.execute("CREATE SCHEMA " + identifier(schema))
        tables = []
        for output in manifest["outputs"]:
            path = relative_file(folder, output["path"])
            if output["format"] not in {"parquet", "jsonl"}:
                continue
            name = schema + "." + output["name"]
            reader = "read_parquet(?)" if output["format"] == "parquet" else "read_json_auto(?)"
            self.connection.execute("CREATE TABLE " + qualified(name) + " AS SELECT * FROM " + reader, [str(path)])
            if output["name"] == "images":
                for image in (o for o in manifest["outputs"] if o["kind"] == "image"):
                    if not image.get("original_ref"):
                        raise RuntimeFailure("IMAGE_INVALID", "Image artifact needs an original_ref mapping.")
                    dest = self.root / "inputs" / (str(uuid4()) + "." + image["format"])
                    shutil.copyfile(relative_file(folder, image["path"]), dest)
                    self.connection.execute("UPDATE " + qualified(name) + " SET path=? WHERE original_ref=?", [str(dest), image["original_ref"]])
                for (image_path,) in self.connection.execute("SELECT path FROM " + qualified(name)).fetchall():
                    if not contained(self.root, image_path).is_file():
                        raise RuntimeFailure("IMAGE_INVALID", "Image table has no verified local byte mapping.")
            tables.append(self.table_summary(name, {"kind": "artifact", "artifact_id": artifact_id, "scope_id": scope, "knowledge_id": source.get("knowledge_id"), "sha256": output["sha256"]}))
        return {"tables": tables, "artifacts": [{"artifact_id": artifact_id, "manifest": str(folder / "manifest.json")}], "warnings": []}

    def recipe(self, package, args, assets):
        lock = Path(__file__).resolve().parents[2] / "uv.lock"
        if not lock.is_file():
            lock = Path(__file__).parent / "uv.lock"
        # Snapshot all user tables, including SQL-created tables not yet ctx.publish'ed.
        table_hashes = {}
        names = self.connection.execute("SELECT table_schema || '.' || table_name FROM information_schema.tables WHERE table_schema NOT LIKE '_vane%' AND table_schema NOT LIKE 'run_%' AND table_name != 'input_files' ORDER BY 1").fetchall()
        for (name,) in names:
            meta = self.table_summary(name)
            table_hashes[name] = meta["content_digest"]
        recipe = {"scope_id": self.scope,
                  "inputs": [{k: a[k] for k in ("sha256", "media_type", "original_name")} | {"title": a.get("title", a["original_name"])} for a in assets],
                  "input_tables": table_hashes, "package_digest": package.digest,
                  "pipeline": args["pipeline"], "params": args.get("params", {}),
                  "dynamic_code": [h["request_digest"] for h in self.history if h["action"] == "execute" and h["request"].get("mode") == "python" and h["status"] == "succeeded"],
                  "host_config": self.config,
                  "vane_version": vane.__version__, "host_version": self.config["hostVersion"], "runtime_version": __version__,
                  "environment_versions": self.environment_versions(),
                  "runtime_code_digest": digest([{ "path": p.name, "sha256": sha(p)} for p in sorted(Path(__file__).parent.glob("*.py"))]),
                  "dependency_lock_digest": sha(lock) if lock.is_file() else digest(sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions()))}
        return digest(recipe), recipe

    def pipeline(self, args):
        import jsonschema
        package = self.packages.get(args["package_id"])
        if not package:
            raise RuntimeFailure("PACKAGE_NOT_ALLOWED", "Package was not enabled for this workspace.")
        package.verify()
        spec = package.manifest["pipelines"].get(args["pipeline"])
        if spec is None:
            raise RuntimeFailure("PIPELINE_NOT_FOUND", "Unknown pipeline.")
        params = args.get("params", {})
        if "parameters_schema" in spec:
            validate_schema(params, package.file(spec["parameters_schema"]), "INVALID_ARGUMENT")
        elif params:
            raise RuntimeFailure("INVALID_ARGUMENT", "Undeclared pipeline parameters must be empty.")
        asset_ids = args["asset_ids"]
        if not asset_ids or len(set(asset_ids)) != len(asset_ids) or any(i not in self.assets for i in asset_ids):
            raise RuntimeFailure("ASSET_NOT_FOUND", "Select unique asset IDs from this workspace.")
        assets = [self.assets[i] for i in asset_ids]
        for asset in assets:
            if sha(contained(self.root, asset["path"])) != asset["sha256"]:
                raise RuntimeFailure("SOURCE_SHA_MISMATCH", "A task input has changed.")
        recipe_hash, recipe = self.recipe(package, args, assets)
        cached = self.store.cached(recipe_hash)
        if cached:
            names = [o.get("workspace_table") for o in cached["outputs"] if o["format"] == "parquet"]
            if all(name and self.table_exists(name) for name in names):
                summaries = [self.table_summary(name) for name in names]
                recorded = [o.get("content_digest") for o in cached["outputs"] if o["format"] == "parquet"]
                if [s["content_digest"] for s in summaries] == recorded:
                    return {"tables": summaries, "artifacts": [{"artifact_id": cached["artifact_id"], "manifest": str(self.store.root / cached["artifact_id"] / "manifest.json")}], "reused": True}
            result = self.load_artifact({"artifact_id": cached["artifact_id"], "store_alias": "workspace"})
            result["reused"] = True
            return result
        schema = "run_" + uuid4().hex
        self.connection.execute("CREATE SCHEMA " + identifier(schema))
        self.connection.execute("SET schema = ?", [schema])
        self.connection.execute("CREATE TABLE input_files(asset_id VARCHAR, path VARCHAR, media_type VARCHAR, sha256 VARCHAR, original_name VARCHAR)")
        for asset in assets:
            self.connection.execute("INSERT INTO input_files VALUES (?,?,?,?,?)", [asset[k] for k in ("asset_id", "path", "media_type", "sha256", "original_name")])
        self.connection.execute("CREATE TABLE run_params(name VARCHAR, value_json VARCHAR)")
        for key, value in params.items():
            self.connection.execute("INSERT INTO run_params VALUES (?,?)", [key, canonical(value)])
        for step in spec["steps"]:
            self.check()
            self.connection.execute(package.file(step).read_text())
        package.verify()
        outputs, tables = [], []
        stage = Path(tempfile.mkdtemp(prefix="export-", dir=self.root / "staging"))
        try:
            for logical, table in spec["outputs"].items():
                name = schema + "." + table
                meta = self.table_summary(name, {"kind": "pipeline", "package_id": package.id, "package_digest": package.digest, "asset_ids": asset_ids, "recipe_hash": recipe_hash})
                self.validate_output(logical, name, meta)
                path = stage / (logical + ".parquet")
                self.connection.execute("COPY " + qualified(name) + " TO ? (FORMAT PARQUET)", [str(path)])
                outputs.append({"name": logical, "kind": "evidence" if logical == "evidence" else "document" if logical == "documents" else "table", "format": "parquet", "path": str(path), "rows": meta["row_count"], "workspace_table": name, "content_digest": meta["content_digest"]})
                tables.append(meta)
                if logical == "images":
                    for original_ref, image_path, mime_type in self.connection.execute("SELECT original_ref,path,mime_type FROM " + qualified(name)).fetchall():
                        image = contained(self.root, image_path)
                        if not image.is_file() or mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                            raise RuntimeFailure("IMAGE_INVALID", "Image output must exist in this workspace with a supported MIME type.")
                        from PIL import Image
                        with Image.open(image) as im:
                            im.verify()
                        outputs.append({"name": "image_" + str(len(outputs)), "kind": "image", "format": mime_type.split("/")[1], "path": str(image), "original_ref": original_ref})
            pending = self.store.stage(recipe_hash, {"id": package.id, "version": package.version, "digest": package.digest}, assets, outputs, recipe)
            self.pending_artifacts.append(pending)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        warnings = []
        if params.get("model_alias"):
            warnings.append("Model output is not guaranteed bitwise reproducible; model identity, sampling and prompt/schema are recorded in the recipe.")
        return {"tables": tables, "schema": schema, "recipe_hash": recipe_hash, "warnings": warnings, "reused": False}

    def validate_output(self, logical, name, meta):
        fixed = {"documents": ["document_key", "title", "markdown"], "evidence": ["evidence_id", "asset_id", "locator_json", "quote"], "images": ["original_ref", "path", "mime_type"]}
        if logical in fixed:
            if [s["name"] for s in meta["schema"]] != fixed[logical] or any(s["type"] != "VARCHAR" for s in meta["schema"]):
                raise RuntimeFailure("CONTRACT_INVALID", "Fixed output columns/types do not match vane-package/v1.")
        if logical == "evidence":
            for evidence_id, asset_id, locator, quote in self.connection.execute("SELECT * FROM " + qualified(name)).fetchall():
                if not evidence_id or asset_id not in self.assets or not isinstance(quote, str):
                    raise RuntimeFailure("EVIDENCE_INVALID", "Evidence must refer to a loaded asset and contain a quote.")
                try:
                    loc = json.loads(locator)
                except (ValueError, TypeError):
                    raise RuntimeFailure("EVIDENCE_INVALID", "Evidence locator must be a JSON object.") from None
                if not isinstance(loc, dict) or ("page" in loc and (type(loc["page"]) is not int or loc["page"] < 1)) or ("row" in loc and (type(loc["row"]) is not int or loc["row"] < 1)):
                    raise RuntimeFailure("EVIDENCE_INVALID", "Invalid one-based evidence location.")
                if "page" in loc and self.assets[asset_id]["media_type"] == "application/pdf":
                    import pymupdf
                    with pymupdf.open(self.assets[asset_id]["path"]) as document:
                        if loc["page"] > len(document):
                            raise RuntimeFailure("EVIDENCE_INVALID", "Evidence page exceeds the source PDF page count.")
                if "bbox" in loc:
                    b = loc["bbox"]
                    if not isinstance(b, list) or len(b) != 4 or not all(isinstance(v, (float, int)) and 0 <= v <= 1 for v in b) or b[0] > b[2] or b[1] > b[3]:
                        raise RuntimeFailure("EVIDENCE_INVALID", "Invalid normalized image bounding box.")
                if any(k in loc for k in ("start_ms", "end_ms")) and not (type(loc.get("start_ms")) is int and type(loc.get("end_ms")) is int and 0 <= loc["start_ms"] <= loc["end_ms"]):
                    raise RuntimeFailure("EVIDENCE_INVALID", "Invalid millisecond audio range.")

    def checkpoint(self, args):
        import cloudpickle
        names = args["tables"]
        if not isinstance(names, list) or len(set(names)) != len(names):
            raise RuntimeFailure("INVALID_ARGUMENT", "Checkpoint tables must be an explicit unique list.")
        checkpoint_id = str(uuid4())
        root = self.root.parent / "checkpoints"
        root.mkdir(exist_ok=True)
        stage = root / (".pending-" + checkpoint_id)
        stage.mkdir()
        try:
            tables, files, assets, functions = [], [], [], []
            for i, name in enumerate(names):
                meta = self.table_summary(name)
                path = stage / (str(i) + ".parquet")
                self.connection.execute("COPY " + qualified(name) + " TO ? (FORMAT PARQUET)", [str(path)])
                tables.append({"name": name, "path": path.name, "sha256": sha(path), "provenance": meta["provenance"]})
            for asset in self.assets.values():
                dest = stage / (asset["asset_id"] + Path(asset["path"]).suffix)
                shutil.copyfile(asset["path"], dest)
                if sha(dest) != asset["sha256"]:
                    raise RuntimeFailure("SOURCE_SHA_MISMATCH", "Checkpoint input SHA mismatch.")
                assets.append({**asset, "path": dest.name})
            for name, udf in self.udfs.items():
                if not udf["dynamic"]:
                    continue
                dest = stage / (name + ".udf")
                try:
                    dest.write_bytes(cloudpickle.dumps(udf["fn"]))
                except Exception:
                    raise RuntimeFailure("CHECKPOINT_UNSUPPORTED", "UDF captures a non-serializable object; materialize its results first.") from None
                functions.append({"name": name, "path": dest.name, "sha256": sha(dest), "parameters": udf["parameters"], "return_type": udf["return_type"]})
            history = stage / "history.json"
            write_json(history, self.history)
            files.append({"path": history.name, "sha256": sha(history)})
            manifest = {"schema_version": "dsh-vane-checkpoint/v1", "checkpoint_id": checkpoint_id, "scope_id": self.scope,
                        "created_at": now(), "runtime_version": __version__, "vane_version": vane.__version__, "host_version": self.config["hostVersion"],
                        "environment_digest": digest(self.environment_versions()),
                        "packages": [{"id": p.id, "version": p.version, "digest": p.digest} for p in self.packages.values()],
                        "tables": tables, "assets": assets, "functions": functions, "files": files}
            write_json(stage / "manifest.json", manifest)
            write_json(stage / "manifest.sha256.json", {"sha256": sha(stage / "manifest.json")})
            self.pending_checkpoints.append((stage, root / checkpoint_id))
            return {"checkpoint_id": checkpoint_id, "tables": [{"name": n} for n in names], "warnings": ["Only selected materialized tables, serializable UDFs, input files and execution records are saved; Relations/globals are not."]}
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def restore(self, args):
        import cloudpickle
        folder = contained(self.root.parent / "checkpoints", self.root.parent / "checkpoints" / uuid_value(args["checkpoint_id"]))
        manifest_path = relative_file(folder, "manifest.json")
        if sha(manifest_path) != json.loads(relative_file(folder, "manifest.sha256.json").read_text())["sha256"]:
            raise RuntimeFailure("CHECKPOINT_SHA_MISMATCH", "Checkpoint manifest SHA mismatch.")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") != "dsh-vane-checkpoint/v1" or manifest.get("checkpoint_id") != folder.name or manifest.get("scope_id") != self.scope:
            raise RuntimeFailure("CHECKPOINT_INVALID", "Invalid checkpoint version/identity/scope.")
        if (manifest["runtime_version"], manifest["vane_version"], manifest["host_version"]) != (__version__, vane.__version__, self.config["hostVersion"]):
            raise RuntimeFailure("CHECKPOINT_VERSION_MISMATCH", "Checkpoint runtime/Vane/host version differs.")
        if manifest.get("environment_digest") != digest(self.environment_versions()):
            raise RuntimeFailure("CHECKPOINT_VERSION_MISMATCH", "Checkpoint Python/dependency versions differ; restore in the original locked environment.")
        current = [{"id": p.id, "version": p.version, "digest": p.digest} for p in self.packages.values()]
        if manifest["packages"] != current:
            raise RuntimeFailure("PACKAGE_DIGEST_MISMATCH", "Checkpoint packages differ from this instance.")
        for entry in [*manifest["tables"], *manifest["assets"], *manifest["functions"], *manifest["files"]]:
            if sha(relative_file(folder, entry["path"])) != entry["sha256"]:
                raise RuntimeFailure("CHECKPOINT_SHA_MISMATCH", "Checkpoint content SHA mismatch.")
        tables = []
        for table in manifest["tables"]:
            name = table["name"]
            parts = name.split(".")
            if len(parts) == 2:
                self.connection.execute("CREATE SCHEMA IF NOT EXISTS " + identifier(parts[0]))
            if name in {"input_files", "main.input_files"}:
                continue  # Paths are reconstructed from verified source copies below.
            self.connection.execute("CREATE TABLE " + qualified(name) + " AS SELECT * FROM read_parquet(?)", [str(relative_file(folder, table["path"]))])
            tables.append(self.table_summary(name, table["provenance"]))
        for asset in manifest["assets"]:
            dest = self.root / "inputs" / Path(asset["path"]).name
            shutil.copyfile(relative_file(folder, asset["path"]), dest)
            restored = {**asset, "path": str(dest)}
            self.assets[asset["asset_id"]] = restored
            self.connection.execute("INSERT INTO main.input_files VALUES (?,?,?,?,?)", [restored[k] for k in ("asset_id", "path", "media_type", "sha256", "original_name")])
        for function in manifest["functions"]:
            # Checkpoints are trusted local session artifacts; never unpickle a WeKnora marker.
            fn = cloudpickle.loads(relative_file(folder, function["path"]).read_bytes())
            self.register_udf(function["name"], fn, function["parameters"], function["return_type"], dynamic=True)
        self.history = json.loads(relative_file(folder, "history.json").read_text())
        return {"checkpoint_id": folder.name, "tables": tables, "assets": list(self.assets.values()), "warnings": ["Restored materialized tables and UDFs; process globals and Relations were not restored."]}
