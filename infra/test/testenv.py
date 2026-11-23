"""Configuration of one isolated test environment, derived from its deployment parameters file.

Resource names follow the same convention as main.bicep, so no script embeds
environment-specific names or identifiers.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


PARAMS_ENV = "SHAREIARAG_TEST_PARAMS"
DEFAULT_PARAMS = Path(__file__).resolve().with_name("runtime.parameters.json")
SUFFIX_PATTERN = re.compile(r"[a-z0-9]{3,8}")
# Microsoft Enterprise Token Store: well-known client used by Microsoft 365 Copilot connectors for SSO.
M365_TOKEN_STORE_CLIENT_ID = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b"


@dataclass(frozen=True)
class TestEnv:
    suffix: str
    resource_group: str
    group_a: str = ""
    group_b: str = ""
    console_client_id: str = ""
    download_client_id: str = ""
    mcp_client_id: str = ""
    test_users: tuple[str, ...] = ()

    def __post_init__(self):
        if not SUFFIX_PATTERN.fullmatch(self.suffix or ""):
            raise ValueError("The suffix must be 3-8 lowercase letters or digits.")
        if not self.resource_group or self.resource_group.startswith("<"):
            raise ValueError("The resource group name is missing from the parameters file.")

    @property
    def search(self):
        return f"srch-shareiarag-test-{self.suffix}"

    @property
    def foundry(self):
        return f"aif-shareiarag-test-{self.suffix}"

    @property
    def project(self):
        return f"proj-shareiarag-test-{self.suffix}"

    @property
    def files(self):
        return f"stshragfiles{self.suffix}"

    @property
    def vault(self):
        return f"kv-shrag-test-{self.suffix}"

    @property
    def console_site(self):
        return f"app-shareiarag-test-console-{self.suffix}"

    @property
    def mcp_site(self):
        return f"app-shareiarag-test-mcp-{self.suffix}"

    @property
    def download_site(self):
        return f"func-shareiarag-test-dl-{self.suffix}"

    @property
    def mcp_identity(self):
        return f"id-shareiarag-test-mcp-{self.suffix}"

    @property
    def console_app(self):
        return f"ent-shareiarag-test-console-{self.suffix}"

    @property
    def mcp_app(self):
        return f"ent-shareiarag-test-mcp-{self.suffix}"

    @property
    def download_app(self):
        return f"ent-shareiarag-test-download-{self.suffix}"

    @property
    def easyauth_credential(self):
        return f"shareiarag-test-easyauth-{self.suffix}"

    @property
    def search_endpoint(self):
        return f"https://{self.search}.search.windows.net"

    @property
    def foundry_endpoint(self):
        return f"https://{self.foundry}.openai.azure.com"

    def host_pattern(self, site):
        """Match the real default host of a site, including App Service unique-host variants."""
        return re.compile(re.escape(site) + r"(?:-[a-z0-9]{6,})?(?:\.[a-z0-9-]+-01)?\.azurewebsites\.net")


def load(path=None) -> TestEnv:
    path = Path(path or os.environ.get(PARAMS_ENV) or DEFAULT_PARAMS)
    try:
        parameters = json.loads(path.read_text(encoding="utf-8"))["parameters"]
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Parameters file not found: {path}. Copy runtime.parameters.example.json and fill it in, "
            f"or set {PARAMS_ENV}."
        ) from error

    def value(name, default=""):
        return parameters.get(name, {}).get("value", default)

    return TestEnv(
        suffix=value("suffix"),
        resource_group=value("resourceGroupName"),
        group_a=value("groupAObjectId").lower(),
        group_b=value("groupBObjectId").lower(),
        console_client_id=value("consoleClientId").lower(),
        download_client_id=value("downloadClientId").lower(),
        mcp_client_id=value("mcpClientId").lower(),
        test_users=tuple(user.lower() for user in value("testUserObjectIds", [])),
    )
