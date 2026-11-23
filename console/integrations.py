"""Read-only status of the Microsoft 365 Copilot MCP connector and the Foundry agent, plus MCP start/stop."""
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

from azure_gateway import AzureApiError

# Only these MCP app settings are returned; the others can hold secrets or Key Vault references.
MCP_SETTINGS = (
    "MCP_MODE", "MCP_PUBLIC_HOST", "MCP_CLIENT_ID", "MCP_TOKEN_AUDIENCES", "MCP_ALLOWED_CLIENT_IDS",
    "MCP_REQUIRED_SCOPE", "MCP_FIC_IDENTITY_CLIENT_ID", "SEARCH_ENDPOINT",
)
POWER_ACTIONS = ("start", "stop")
FOUNDRY_VERSION = "2025-06-01"


def _section(load):
    try:
        return {"status": "ok", "data": load()}
    except AzureApiError as error:
        return {"status": "error", "error": str(error)}


def _probe(url, method="GET"):
    try:
        return requests.request(method, url, timeout=(5, 10), allow_redirects=False).status_code
    except requests.RequestException:
        return None


def mcp_status(gateway, settings):
    site_id = settings.resource_id("Microsoft.Web/sites", settings.mcp_app)

    def site():
        value = gateway.arm("GET", site_id)
        props = value.get("properties") or {}
        identity = value.get("identity") or {}
        plan = gateway.arm("GET", props["serverFarmId"]) if props.get("serverFarmId") else {}
        return {
            "name": value["name"], "state": props.get("state"), "host": props.get("defaultHostName"),
            "httpsOnly": props.get("httpsOnly"), "location": value.get("location"),
            "plan": plan.get("name"), "planSku": (plan.get("sku") or {}).get("name"),
            "identity": identity.get("type"),
            "userAssignedClientIds": sorted(
                item.get("clientId") for item in (identity.get("userAssignedIdentities") or {}).values() if item.get("clientId")
            ),
            "portal": f"https://portal.azure.com/#resource{value['id']}",
        }

    def app_settings():
        values = (gateway.arm("POST", site_id + "/config/appsettings/list") or {}).get("properties") or {}
        return {
            "values": {key: values[key] for key in MCP_SETTINGS if key in values},
            "legacyClientSecret": "MCP_CLIENT_SECRET" in values,
        }

    with ThreadPoolExecutor(max_workers=2) as executor:
        site_future, settings_future = executor.submit(_section, site), executor.submit(_section, app_settings)
        app, configured = site_future.result(), settings_future.result()
    host = (app.get("data") or {}).get("host") or f"{settings.mcp_app}.azurewebsites.net"
    running = (app.get("data") or {}).get("state") == "Running"
    return {
        "appName": settings.mcp_app, "endpoint": f"https://{host}/mcp", "tenantId": settings.tenant,
        "app": app, "settings": configured,
        "probes": {
            "health": _probe(f"https://{host}/healthz") if running else None,
            "anonymousMcp": _probe(f"https://{host}/mcp", "POST") if running else None,
        },
    }


def set_mcp_power(gateway, settings, action):
    if action not in POWER_ACTIONS:
        raise ValueError("Choose start or stop.")
    gateway.arm("POST", settings.resource_id("Microsoft.Web/sites", settings.mcp_app) + f"/{action}", expected=(200, 204))


def _project_endpoint(project):
    endpoint = ((project.get("properties") or {}).get("endpoints") or {}).get("AI Foundry API", "")
    parts = urlsplit(endpoint)
    # The endpoint comes from ARM; still refuse anything but a Foundry host before sending a token to it.
    if parts.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.services\.ai\.azure\.com", parts.hostname or ""):
        raise AzureApiError("The Foundry project endpoint is missing or unexpected.")
    return endpoint.rstrip("/")


def _agent(item):
    latest = (item.get("versions") or {}).get("latest") or {}
    definition = latest.get("definition") or {}
    return {
        "name": item.get("name"), "version": latest.get("version"), "kind": definition.get("kind"),
        "model": definition.get("model"), "hasInstructions": bool(definition.get("instructions")),
        "tools": [{
            "type": tool.get("type"), "label": tool.get("server_label"),
            "connection": tool.get("project_connection_id"), "approval": tool.get("require_approval"),
        } for tool in definition.get("tools") or []],
    }


def foundry_status(gateway, settings):
    account_id = settings.resource_id("Microsoft.CognitiveServices/accounts", settings.foundry_account)
    project_path = f"{account_id}/projects/{settings.foundry_project}"

    def deployments():
        result = gateway.arm("GET", account_id + "/deployments", version="2024-10-01") or {}
        return [{
            "name": item["name"], "model": ((item.get("properties") or {}).get("model") or {}).get("name"),
            "version": ((item.get("properties") or {}).get("model") or {}).get("version"),
            "sku": (item.get("sku") or {}).get("name"), "capacity": (item.get("sku") or {}).get("capacity"),
        } for item in result.get("value", [])]

    def connections():
        result = gateway.arm("GET", project_path + "/connections", version=FOUNDRY_VERSION) or {}
        return [{
            "name": item["name"], "category": (item.get("properties") or {}).get("category"),
            "authType": (item.get("properties") or {}).get("authType"),
            "target": (item.get("properties") or {}).get("target"),
        } for item in result.get("value", [])]

    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {
            "account": executor.submit(_section, lambda: gateway.resource(
                "Microsoft.CognitiveServices/accounts", settings.foundry_account, "2024-10-01")),
            "project": executor.submit(_section, lambda: gateway.arm("GET", project_path, version=FOUNDRY_VERSION)),
            "deployments": executor.submit(_section, deployments),
            "connections": executor.submit(_section, connections),
        }
        sections = {name: future.result() for name, future in pending.items()}
    project = sections["project"]
    if project["status"] == "ok":
        raw = project["data"]
        try:
            endpoint = _project_endpoint(raw)
            sections["project"] = {"status": "ok", "data": {
                "name": settings.foundry_project, "endpoint": endpoint, "location": raw.get("location"),
                "portal": f"https://ai.azure.com/resource/overview?wsid={raw['id']}",
            }}
            sections["agents"] = _section(lambda: [_agent(item) for item in (gateway.request(
                "GET", f"{endpoint}/agents?api-version=v1", "https://ai.azure.com/.default") or {}).get("data", [])])
        except AzureApiError as error:
            sections["project"] = {"status": "error", "error": str(error)}
    sections.setdefault("agents", {"status": "error", "error": "The Foundry project is unavailable."})
    return {
        "accountName": settings.foundry_account, "projectName": settings.foundry_project,
        "agentName": settings.foundry_agent, "knowledgeBase": settings.knowledge_base,
        "searchService": settings.search_name, "sections": sections,
    }
