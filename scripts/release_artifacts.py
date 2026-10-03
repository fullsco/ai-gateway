"""Build auditable baseline/current wheels from explicit Git revisions, out of tree."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REVISIONS = {"baseline": "230183eabce52d717cc53bc4cc3a66ec80abfa2a",
             "current": "9ebe1821529bc8607fd754ba736e2bfe601e9e73"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(root, output):
    output.mkdir(exist_ok=False)
    manifest = {"versions": {}, "baseline_scope": "committed 230183e; excludes unrecorded changes"}
    for label, revision in REVISIONS.items():
        target = output / label
        source = target / "source"
        source.mkdir(parents=True)
        archive = subprocess.check_output(["git", "archive", revision, "gateway",
            "pyproject.toml", "requirements.lock"], cwd=root, timeout=30)
        files = {}
        with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
            for member in tree:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                    raise ValueError("Unsafe artifact source entry")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("Unsupported artifact source entry")
                data = tree.extractfile(member).read()
                destination = source / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                if path.parts[0] == "gateway":
                    files[str(path)] = digest(data)
        epoch = subprocess.check_output(["git", "show", "-s", "--format=%ct", revision],
                                        cwd=root, timeout=10).decode().strip()
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(target)],
                       cwd=source, env={**os.environ, "SOURCE_DATE_EPOCH": epoch}, check=True)
        wheel, = target.glob("*.whl")
        with zipfile.ZipFile(wheel) as package:
            packaged = {name: digest(package.read(name)) for name in package.namelist()
                        if name.startswith("gateway/") and not name.endswith("/")}
            if packaged != files:
                raise ValueError("Wheel code differs from the pinned Git tree")
        lock = (source / "requirements.lock").read_bytes()
        (target / "requirements.lock").write_bytes(lock)
        manifest["versions"][label] = {"revision": revision, "wheel": f"{label}/{wheel.name}",
            "wheel_sha256": digest(wheel.read_bytes()), "gateway_files": files,
            "requirements_sha256": digest(lock)}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({label: {k: value for k, value in version.items() if k != "gateway_files"}
                      for label, version in manifest["versions"].items()}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to("/tmp/opencode"):
        parser.error("Artifact output must be beneath /tmp/opencode")
    build(Path(__file__).resolve().parents[1], args.output.resolve())
