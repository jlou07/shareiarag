import os
import re
from dataclasses import dataclass
from uuid import UUID


def identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("A UUID string is required.")
    return str(UUID(value))


@dataclass(frozen=True)
class Settings:
    subscription: str
    tenant: str
    client_id: str
    administrators: frozenset[str]
    resource_group: str = "rg-shareiarag"
    search_name: str = "srch-shareiarag"
    search_version: str = "2026-08-01-preview"
    knowledge_source: str = "af-ks-all"
    knowledge_base: str = "af-kb-all"
    files_account: str = "shareiarag"
    foundry_account: str = "aif-shareiarag"
    download_app: str = "func-shareiarag-dl"
    state_account: str = "stshareiaragfn"
    state_container: str = "rag-console-e8e7"
    mcp_app: str = "app-shareiarag-mcp-dev-68e5"
    foundry_project: str = "proj-shareiarag"
    foundry_agent: str = "agent-shareiarag"
    hosting_plan_id: str = ""
    key_vault_name: str = ""
    directory_enabled: bool = False

    @classmethod
    def from_env(cls):
        admins = frozenset(identifier(v.strip()) for v in os.environ["ADMIN_OBJECT_IDS"].split(",") if v.strip())
        if not admins:
            raise ValueError("At least one console administrator is required.")
        values = {
            "subscription": identifier(os.environ["AZURE_SUBSCRIPTION_ID"]),
            "tenant": identifier(os.environ["AZURE_TENANT_ID"]),
            "client_id": identifier(os.environ["ENTRA_CLIENT_ID"]),
            "administrators": admins,
        }
        for field, variable in (
            ("resource_group", "AZURE_RESOURCE_GROUP"), ("search_name", "SEARCH_SERVICE_NAME"),
            ("knowledge_source", "KNOWLEDGE_SOURCE_NAME"), ("knowledge_base", "KNOWLEDGE_BASE_NAME"),
            ("files_account", "FILES_ACCOUNT_NAME"), ("foundry_account", "FOUNDRY_ACCOUNT_NAME"),
            ("download_app", "DOWNLOAD_APP_NAME"), ("state_account", "STATE_ACCOUNT_NAME"),
            ("state_container", "STATE_CONTAINER_NAME"), ("mcp_app", "MCP_APP_NAME"),
            ("foundry_project", "FOUNDRY_PROJECT_NAME"), ("foundry_agent", "FOUNDRY_AGENT_NAME"),
        ):
            value = os.environ.get(variable, cls.__dataclass_fields__[field].default)
            if not re.fullmatch(r"[a-zA-Z0-9._()-]{1,90}", value):
                raise ValueError(f"{variable} is invalid.")
            values[field] = value
        values["search_version"] = os.environ.get("SEARCH_API_VERSION", cls.search_version)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-preview)?", values["search_version"]):
            raise ValueError("SEARCH_API_VERSION is invalid.")
        values["hosting_plan_id"] = os.environ["HOSTING_PLAN_ID"]
        if not re.fullmatch(
            rf"/subscriptions/{values['subscription']}/resourceGroups/[\w.()-]+/providers/Microsoft.Web/serverfarms/[\w.-]+",
            values["hosting_plan_id"], re.IGNORECASE,
        ):
            raise ValueError("HOSTING_PLAN_ID must reference a plan in the configured subscription.")
        values["directory_enabled"] = os.environ.get("GROUP_DIRECTORY_ENABLED", "false").lower() == "true"
        values["key_vault_name"] = os.environ.get("KEY_VAULT_NAME", "")
        if values["key_vault_name"] and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]", values["key_vault_name"]):
            raise ValueError("KEY_VAULT_NAME is invalid.")
        if os.environ.get("AUTH_MODE", "entra") != "entra":
            raise ValueError("Only Entra authentication is supported.")
        return cls(**values)

    @property
    def group_id(self):
        return f"/subscriptions/{self.subscription}/resourceGroups/{self.resource_group}"

    def resource_id(self, provider: str, name: str):
        return f"{self.group_id}/providers/{provider}/{name}"
