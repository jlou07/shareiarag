"""Prepare Foundry agent instructions using only the isolated test endpoints."""

import argparse
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

import testenv


SOURCE = Path(__file__).resolve().parents[2] / "agent-instructions.txt"


def render_instructions(env, source: str, download_base: str) -> str:
    parsed = urlsplit(download_base)
    test_account = env.files
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not env.host_pattern(env.download_site).fullmatch(parsed.hostname)
        or parsed.netloc != parsed.hostname
        or parsed.path.rstrip("/") != "/api/f"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use the HTTPS /api/f URL of the isolated test Function.")
    download_base = f"https://{parsed.hostname}/api/f"

    replacements = (
        (
            r"(?m)^- For the original [^\n]+ sources, replace only [^\n]+\.$",
            f"- For the original {test_account}/admin-share and {test_account}/alluser-share sources, "
            f"replace only https://{test_account}.file.core.windows.net/ with {download_base}/.",
        ),
        (
            r"(?m)^- For other sources, convert https://<account>\.file\.core\.windows\.net/[^\n]+\.$",
            f"- For other sources, convert https://<account>.file.core.windows.net/<share>/<file-path> "
            f"to {download_base}/accounts/<account>/<share>/<file-path>. "
            "The file service independently verifies the configured account/share group; "
            "an indexed URL does not itself grant file access.",
        ),
    )
    result = source
    for pattern, replacement in replacements:
        result, count = re.subn(pattern, lambda _: replacement, result)
        if count != 1:
            raise ValueError("The source link instructions have changed; review them before publishing.")
    for host in re.findall(r"https://([a-z0-9.-]+\.azurewebsites\.net)", result):
        if host != parsed.hostname:
            raise ValueError("The instructions still contain a different App Service endpoint.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--params", help=f"Parameters file (default: ${testenv.PARAMS_ENV} or runtime.parameters.json).")
    parser.add_argument("--download-base", required=True, help="Verified test Function URL ending in /api/f.")
    parser.add_argument("--output", type=Path, required=True, help="New file for the test instructions.")
    args = parser.parse_args()

    source_bytes = SOURCE.read_bytes()
    result = render_instructions(testenv.load(args.params), source_bytes.decode("utf-8"), args.download_base).encode("utf-8")
    with args.output.open("xb") as file:
        file.write(result)
    print(f"Source SHA-256: {hashlib.sha256(source_bytes).hexdigest()}")
    print(f"Test instructions SHA-256: {hashlib.sha256(result).hexdigest()}")


if __name__ == "__main__":
    main()
