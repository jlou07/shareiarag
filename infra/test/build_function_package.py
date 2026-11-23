"""Build a download Function package that pins the trusted tenant and client ID.

The versioned source only holds zero-UUID placeholders (which reject every caller);
this script writes the real values into the package, never into download-func.
"""

import argparse
import ast
import hashlib
import io
from pathlib import Path
import stat
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


SOURCE_DIR = Path(__file__).resolve().parents[2] / "download-func"


def _assignment(tree: ast.Module, name: str) -> ast.Assign:
    matches = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    if (
        len(matches) != 1
        or len(matches[0].targets) != 1
        or matches[0].col_offset != 0
        or matches[0].lineno != matches[0].end_lineno
    ):
        raise ValueError(f"Expected exactly one single-line assignment for {name}.")
    return matches[0]


def _canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def pin_test_identity(source: str, client_id: str, tenant_id: str) -> str:
    try:
        client_uuid = UUID(client_id)
        tenant_uuid = UUID(tenant_id)
    except (ValueError, AttributeError) as error:
        raise ValueError("The application client ID and tenant ID must be UUIDs.") from error
    if not client_uuid.int or not tenant_uuid.int:
        raise ValueError("The application client ID and tenant ID must not be the zero UUID.")
    client_id, tenant_id = str(client_uuid), str(tenant_uuid)

    tree = ast.parse(source)
    tenant = _assignment(tree, "TRUSTED_TENANT_ID")
    audiences = _assignment(tree, "TRUSTED_AUDIENCES")
    try:
        original_tenant = ast.literal_eval(tenant.value)
        original_audiences = ast.literal_eval(audiences.value)
    except (ValueError, TypeError) as error:
        raise ValueError("The source trust settings are not literal values.") from error
    if not _canonical_uuid(original_tenant) or not isinstance(original_audiences, set):
        raise ValueError("The source trust settings have changed; review them before packaging.")
    plain_audiences = {value for value in original_audiences if isinstance(value, str) and not value.startswith("api://")}
    if len(plain_audiences) != 1:
        raise ValueError("The source audiences are not in the expected format.")
    original_client_id = plain_audiences.pop()
    try:
        if str(UUID(original_client_id)) != original_client_id:
            raise ValueError
    except (ValueError, AttributeError) as error:
        raise ValueError("The source audience is not a canonical application ID.") from error
    if original_audiences != {original_client_id, f"api://{original_client_id}"}:
        raise ValueError("The source audiences have changed; review them before packaging.")
    placeholder = str(UUID(int=0))
    if original_tenant != placeholder or original_client_id != placeholder:
        raise ValueError("The source must keep zero-UUID placeholders; never commit a real tenant or client ID.")

    lines = source.splitlines(keepends=True)
    lines[tenant.lineno - 1] = f'TRUSTED_TENANT_ID = "{tenant_id}"\n'
    lines[audiences.lineno - 1] = (
        f'TRUSTED_AUDIENCES = {{"{client_id}", "api://{client_id}"}}\n'
    )
    result = "".join(lines)
    ast.parse(result)
    return result


def build_package(client_id: str, tenant_id: str, output: Path, source_dir: Path = SOURCE_DIR) -> tuple[str, str]:
    source_dir = source_dir.resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("The output must be a .zip file.")
    if output.is_relative_to(source_dir):
        raise ValueError("The package must be outside the Function source directory.")

    source_bytes = (source_dir / "function_app.py").read_bytes()
    source = source_bytes.decode("utf-8")
    pinned = pin_test_identity(source, client_id, tenant_id)
    package = io.BytesIO()
    with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
        contents = {
            "function_app.py": pinned.encode("utf-8"),
            **{name: (source_dir / name).read_bytes() for name in ("host.json", "requirements.txt")},
        }
        for name, content in contents.items():
            entry = ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, content)

    payload = package.getvalue()
    with output.open("xb") as file:
        file.write(payload)
    return hashlib.sha256(source_bytes).hexdigest(), hashlib.sha256(payload).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--params", help="Parameters file providing downloadClientId (default: runtime.parameters.json).")
    parser.add_argument("--client-id", help="Client ID of the download application (default: downloadClientId from --params).")
    parser.add_argument("--tenant-id", help="Entra tenant ID (default: tenant selected in Azure CLI).")
    parser.add_argument("--output", type=Path, required=True, help="New .zip path outside download-func.")
    args = parser.parse_args()
    client_id = args.client_id
    if not client_id:
        import testenv
        client_id = testenv.load(args.params).download_client_id
    tenant_id = args.tenant_id
    if not tenant_id:
        from bootstrap_search import account
        tenant_id = account()[1]
    source_hash, package_hash = build_package(client_id, tenant_id, args.output)
    print(f"Source SHA-256: {source_hash}")
    print(f"Package SHA-256: {package_hash}")


if __name__ == "__main__":
    main()
