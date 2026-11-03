"""Write requirements.lock: every package tenant-service's image installs, pinned by version and SHA-256, for
Python 3.12 on Linux, amd64 and arm64. The Dockerfile installs it with pip --require-hashes.

    python services/tenant-service/lock.py          (on Linux, e.g. in a python:3.12 container; nothing is installed)

Run it on Linux: pip reads environment markers (uvloop only off Windows, tzdata only on Windows) from the machine
it runs on, even when resolving for another platform. Dependabot proposes updates to the lock between runs.
"""

import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent.parent / "libs" / "taraz-auth"
PLATFORMS = ("x86_64", "aarch64")


def wanted() -> list[str]:
    """Both packages' dependencies (taraz-auth with its fastapi extra) and the build backend, hatchling."""
    service = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))
    lib = tomllib.loads((LIB / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d for d in service["project"]["dependencies"] if not d.startswith("taraz-auth")]
    deps += lib["project"]["dependencies"] + lib["project"]["optional-dependencies"]["fastapi"]
    return deps + service["build-system"]["requires"]


def resolve(platform: str, deps: list[str], work: Path) -> list[dict]:
    report = work / f"report-{platform}.json"
    tags = [f"--platform={t}_{platform}" for t in ("manylinux2014", "manylinux_2_17", "manylinux_2_28")]
    subprocess.run([sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--ignore-installed",
                    "--only-binary=:all:", *tags, "--python-version=3.12", "--implementation=cp", "--abi=cp312",
                    f"--target={work / 't'}", f"--report={report}", *deps], check=True)
    return json.loads(report.read_text(encoding="utf-8"))["install"]


def main() -> None:
    if sys.platform != "linux":
        sys.exit("run this on Linux: pip reads environment markers from this machine")
    pins: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for platform in PLATFORMS:
            for item in resolve(platform, wanted(), Path(tmp)):
                name = item["metadata"]["name"].lower().replace("_", "-")
                pin = pins.setdefault(name, {"version": item["metadata"]["version"], "hashes": set()})
                if pin["version"] != item["metadata"]["version"]:
                    sys.exit(f"{name}: {pin['version']} on one platform, {item['metadata']['version']} on another")
                pin["hashes"].add(item["download_info"]["archive_info"]["hashes"]["sha256"])
    lines = ["# tenant-service's image installs exactly these (pip install --require-hashes): Python 3.12 on Linux,",
             "# amd64 and arm64. Written by lock.py; Dependabot proposes updates.", ""]
    for name in sorted(pins):
        hashes = sorted(pins[name]["hashes"])
        lines.append(f"{name}=={pins[name]['version']} \\")
        lines += [f"    --hash=sha256:{h}" + (" \\" if n < len(hashes) - 1 else "") for n, h in enumerate(hashes)]
    (HERE / "requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote requirements.lock: {len(pins)} packages")


if __name__ == "__main__":
    main()
