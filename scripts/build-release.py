#!/usr/bin/python3
"""Build a deterministic release using only allowlisted files from a commit."""

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "bin/al", "README.md", "LICENSE", "docs/usage.md",
    "docs/agent-instructions.md",
)


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    commit = git("rev-parse", "--verify", f"{args.ref}^{{commit}}").decode().strip()
    files = {name: git("show", f"{commit}:{name}") for name in FILES}
    version = re.search(rb'^VERSION = "(\d+\.\d+\.\d+)"$', files["bin/al"], re.M)
    if version is None:
        parser.error("bin/al must declare a semantic VERSION")
    version = version[1].decode()
    if args.ref.startswith("v") and args.ref != f"v{version}":
        parser.error("release tag and VERSION differ")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"alog-v{version}-linux-any.tar.gz"
    with archive.open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for name, content in files.items():
                    info = tarfile.TarInfo(name)
                    info.mode = 0o755 if name == "bin/al" else 0o644
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                alias = tarfile.TarInfo("bin/alog")
                alias.type = tarfile.SYMTYPE
                alias.linkname = "al"
                alias.mode = 0o777
                tar.addfile(alias)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with (args.output / "SHA256SUMS").open("x") as stream:
        stream.write(f"{digest}  {archive.name}\n")
    print(f"{archive}\ncommit={commit}\nsha256={digest}")


if __name__ == "__main__":
    main()
