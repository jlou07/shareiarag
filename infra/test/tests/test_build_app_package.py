from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))
import build_app_package as builder


class AppPackageTests(unittest.TestCase):
    def test_console_package_contains_only_allowlisted_files_and_new_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "console.zip"
            with patch.object(builder, "_console_bundle", return_value=b"fresh-test-bundle"):
                hashes, digest = builder.build_package("console", output)
            with ZipFile(output) as archive:
                expected = {Path(name).as_posix() for name in builder.CONSOLE_FILES} | {"static/app.bundle.js"}
                self.assertEqual(set(archive.namelist()), expected)
                self.assertEqual(archive.read("static/app.bundle.js"), b"fresh-test-bundle")
            self.assertEqual(len(hashes), len(builder.CONSOLE_FILES) + 1)
            self.assertEqual(len(digest), 64)
            with self.assertRaises(FileExistsError):
                builder.build_package("console", output)

    def test_mcp_package_does_not_include_env_or_test_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mcp.zip"
            builder.build_package("mcp", output)
            with ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(builder.MCP_FILES))
        with self.assertRaisesRegex(ValueError, "outside"):
            builder.build_package("mcp", builder.ROOT / "mcp-proto" / "not-created.zip")


if __name__ == "__main__":
    unittest.main()
