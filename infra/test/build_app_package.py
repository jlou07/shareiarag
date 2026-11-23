"""Build a test-only console or MCP zip from an explicit source allowlist."""

import argparse
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[2]
CONSOLE_FILES = (
    "app.py", "web.py", "settings.py", "auth.py", "azure_gateway.py", "state.py",
    "operations.py", "index_rebuild.py", "source_connections.py", "documents.py",
    "sync_files.py", "integrations.py", "requirements.txt", r"static\index.html",
)
MCP_FILES = ("server.py", "cloud_app.py", "requirements.txt")


def _console_bundle(console_dir: Path) -> bytes:
    executable = console_dir / "node_modules" / ".bin" / "esbuild.cmd"
    if not executable.is_file():
        raise RuntimeError("The existing console esbuild dependency is unavailable; install dependencies separately.")
    with tempfile.TemporaryDirectory(prefix="shareiarag-test-ui-") as directory:
        bundle = Path(directory) / "app.bundle.js"
        result = subprocess.run(
            [str(executable), str(console_dir / "frontend" / "main.js"), "--bundle", "--minify",
             "--format=iife", f"--outfile={bundle}"],
            cwd=console_dir, capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError(f"Console frontend build failed: {result.stderr.strip()}")
        return bundle.read_bytes()


def build_package(component: str, output: Path, root: Path = ROOT):
    if component not in ("console", "mcp"):
        raise ValueError("Choose console or mcp.")
    source_dir = (root / ("console" if component == "console" else "mcp-proto")).resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip" or output.is_relative_to(source_dir):
        raise ValueError("The output must be a .zip outside the original component directory.")
    if output.exists():
        raise FileExistsError(f"The output already exists: {output}")

    names = CONSOLE_FILES if component == "console" else MCP_FILES
    contents = {Path(name).as_posix(): (source_dir / name).read_bytes() for name in names}
    if component == "console":
        contents["static/app.bundle.js"] = _console_bundle(source_dir)

    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    payload = buffer.getvalue()
    with output.open("xb") as file:
        file.write(payload)
    return {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}, hashlib.sha256(payload).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("console", "mcp"))
    parser.add_argument("--output", required=True, type=Path, help="New .zip path outside the source directory.")
    args = parser.parse_args()
    hashes, package_hash = build_package(args.component, args.output)
    for name, digest in hashes.items():
        print(f"{digest}  {name}")
    print(f"Package SHA-256: {package_hash}")


if __name__ == "__main__":
    main()
