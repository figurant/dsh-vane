"""Compute a vane-package/v1 digest. --write updates a host pin after review."""
import argparse
from pathlib import Path
from dsh_vane_runtime.packages import package_digest

parser = argparse.ArgumentParser()
parser.add_argument("path", nargs="?", default="packages/document_observations")
parser.add_argument("--write", action="store_true")
args = parser.parse_args()
value, _ = package_digest(args.path)
if args.write:
    (Path(args.path) / "digest.sha256").write_text(value + "\n")
print(value)
