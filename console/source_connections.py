import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from azure_gateway import AzureApiError


PREFIX = "rag-control:"
INTERVALS = ("PT5M", "PT15M", "PT30M", "PT1H")


class SourcePrerequisiteError(ValueError):
    def __init__(self, code, message, setting=None):
        super().__init__(message)
        self.code = code
        self.setting = setting


def account_path(settings, resource_group, account):
    if not isinstance(resource_group, str) or not re.fullmatch(r"[\w.()-]{1,90}", resource_group):
        raise ValueError("Choose a valid resource group.")
    if not isinstance(account, str) or not re.fullmatch(r"[a-z0-9]{3,24}", account):
        raise ValueError("Choose a valid storage account.")
    return f"/subscriptions/{settings.subscription}/resourceGroups/{resource_group}/providers/Microsoft.Storage/storageAccounts/{account}"


def validate_share(share):
    if not isinstance(share, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", share) or "--" in share:
        raise ValueError("Choose a valid Azure Files share.")
    return share


def source_identity(account, share):
    return f"{account}/{share}"


def names(account, share, index):
    suffix = hashlib.sha256(source_identity(account, share).encode()).hexdigest()[:20]
    slot = "b" if index == "af-index-all-b" else "a"
    return {"source": f"rc-ds-{suffix}", "skillset": f"rc-skills-{suffix}-{slot}", "name": f"rc-indexer-{suffix}-{slot}"}


def metadata(settings, data):
    description = data.get("description") or ""
    if description.startswith(PREFIX):
        try:
            value = json.loads(description[len(PREFIX):])
            account_path(settings, value["resourceGroup"], value["account"])
            if value["subscriptionId"] != settings.subscription:
                raise ValueError("Unexpected subscription.")
        except (ValueError, KeyError, TypeError) as error:
            raise AzureApiError("A source contains invalid resource metadata.") from error
    elif data.get("name") in ("af-ds-admin", "af-ds-alluser"):
        value = {"subscriptionId": settings.subscription, "resourceGroup": settings.resource_group, "account": settings.files_account}
    else:
        raise AzureApiError("An unmanaged source needs reviewed resource metadata before console administration.")
    share = data.get("container", {}).get("name")
    try:
        validate_share(share)
    except ValueError as error:
        raise AzureApiError("A source has an invalid share name.") from error
    key = share if not description.startswith(PREFIX) else source_identity(value["account"], share)
    return {**{field: value[field] for field in ("subscriptionId", "resourceGroup", "account")}, "sourceKey": key}


def secret_name(account):
    return f"files-connection-{account}"


def connection(account):
    variable = f"FILES_CONNECTION_{account.upper()}"
    value = os.environ.get(variable, "")
    if value.startswith("@Microsoft.KeyVault("):
        raise SourcePrerequisiteError(
            "storage_connection_unresolved",
            f"The Key Vault reference in {variable} is not resolved. Grant the console identity "
            f"Key Vault Secrets User on secret {secret_name(account)}, then restart the console.",
            setting=variable,
        )
    pairs = [part.split("=", 1) for part in value.split(";") if part]
    parts = dict(pair for pair in pairs if len(pair) == 2)
    if (
        parts.get("AccountName") != account or not parts.get("AccountKey")
        or parts.get("DefaultEndpointsProtocol") != "https"
        or parts.get("EndpointSuffix", "core.windows.net") != "core.windows.net"
        or len(parts) != len(pairs)
        or set(parts) - {"DefaultEndpointsProtocol", "AccountName", "AccountKey", "EndpointSuffix"}
    ):
        raise SourcePrerequisiteError(
            "missing_storage_connection",
            f"Store the account connection in Key Vault secret {secret_name(account)} and reference it "
            f"from server setting {variable} before connecting this account.",
            setting=variable,
        )
    return value, parts["AccountKey"]


def skillset(template, name, index, group, share):
    from index_rebuild import _skillset_copy

    body = _skillset_copy(template, name, index)
    updated = set()
    for skill in body.get("skills", []):
        if skill.get("name") in ("group-id", "share-name"):
            for item in skill.get("inputs", []):
                if item.get("name") == "whenTrue":
                    item["source"] = f"= '{group if skill['name'] == 'group-id' else share}'"
                    updated.add(skill["name"])
    if updated != {"group-id", "share-name"}:
        raise AzureApiError("The source template must contain reviewed group-id and share-name skills.")
    return body


def step(gateway, operation):
    from azure_gateway import editable, group_from_skillset, parse_share_groups
    from state import StateConflict, now

    op = copy.deepcopy(operation)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(op["deadline"]):
        raise StateConflict("Source connection timed out. Inspect Azure and resume explicitly.")
    phase = op["phase"]
    slots = ("af-index-all", "af-index-all-b")
    connection_string, _ = connection(op["account"])
    if phase == "prepare":
        sources = gateway.sources()
        if sources["activeIndex"] != op["activeIndex"]:
            raise StateConflict("The active index changed before source connection.")
        gateway.verify_storage_share(op["resourceGroup"], op["account"], op["share"])
        op.update(status="running", phase="datasource")
    elif phase == "datasource":
        name = names(op["account"], op["share"], op["activeIndex"])["source"]
        path = f"/datasources/{name}"
        description = PREFIX + json.dumps({key: op[key] for key in ("subscriptionId", "resourceGroup", "account")}, sort_keys=True)
        current = gateway.search("GET", path, expected=(200, 404))
        if current is None:
            gateway.search("PUT", path, {
                "name": name, "description": description, "type": "azurefile",
                "credentials": {"connectionString": connection_string}, "container": {"name": op["share"]},
            }, {"If-None-Match": "*"}, (201,))
        elif (
            current.get("description") != description or current.get("type") != "azurefile"
            or current.get("container", {}).get("name") != op["share"] or current.get("container", {}).get("query")
        ):
            raise StateConflict("The target Search data source already has a different configuration.")
        op.update(phase="skills", cursor=0)
    elif phase == "skills":
        index = slots[op["cursor"]]
        target = names(op["account"], op["share"], index)
        path = f"/skillsets/{target['skillset']}"
        if gateway.search("GET", f"/indexes/{index}", expected=(200, 404)) is None:
            # A fresh deployment has no standby index until the first rebuild; skillset projections need it.
            from operations import sanitized_schema

            schema = sanitized_schema(gateway.search("GET", f"/indexes/{op['activeIndex']}"))
            schema["name"] = index
            gateway.search("PUT", f"/indexes/{index}", schema, {"If-None-Match": "*"}, (201,))
        template = op["template"]["skillset"] if op.get("template") else gateway.search("GET", f"/skillsets/{op['templateSkillset']}")
        desired = skillset(template, target["skillset"], index, op["groupId"], op["share"])
        current = gateway.search("GET", path, expected=(200, 404))
        if current is None:
            gateway.search("PUT", path, desired, {"If-None-Match": "*"}, (201,))
        elif editable(current) != desired:
            raise StateConflict("A target skillset has changed. Review it before resuming the connection.")
        if group_from_skillset(gateway.search("GET", path)) != op["groupId"]:
            raise AzureApiError("The new source permission skill could not be verified.")
        op["cursor"] += 1
        if op["cursor"] == len(slots):
            op.update(phase="indexers", cursor=0)
    elif phase == "indexers":
        index = slots[op["cursor"]]
        target = names(op["account"], op["share"], index)
        path = f"/indexers/{target['name']}"
        body = editable(op["template"]["indexer"] if op.get("template") else gateway.search("GET", f"/indexers/{op['templateIndexer']}"))
        body.update(name=target["name"], dataSourceName=target["source"], skillsetName=target["skillset"], targetIndexName=index)
        body.pop("schedule", None)
        body["disabled"] = True
        current = gateway.search("GET", path, expected=(200, 404))
        if current is None:
            gateway.search("PUT", path, body, {"If-None-Match": "*"}, (201,))
        elif editable(current) != body:
            raise StateConflict("A target indexer has changed. Review it before resuming the connection.")
        op["cursor"] += 1
        if op["cursor"] == len(slots):
            op["phase"] = "download"
    elif phase == "download":
        gateway.publish_source_download(op["account"], op["share"], op["groupId"])
        op["phase"] = "activate"
    elif phase == "activate":
        name = names(op["account"], op["share"], op["activeIndex"])["name"]
        path = f"/indexers/{name}"
        current = gateway.search("GET", path)
        if current.get("disabled"):
            body = editable(current)
            body["disabled"] = False
            gateway.search("PUT", path, body, {"If-Match": current["@odata.etag"]}, (200, 201, 204))
        if gateway.search("GET", path).get("disabled"):
            raise AzureApiError("The new source indexer could not be enabled.")
        op["phase"] = "run"
    elif phase == "run":
        name = names(op["account"], op["share"], op["activeIndex"])["name"]
        result = gateway.search("GET", f"/indexers/{name}/status").get("lastResult") or {}
        if result.get("status") != "inProgress":
            op["previousStart"] = result.get("startTime")
            gateway.search("POST", f"/indexers/{name}/run", expected=(202,))
        op["phase"] = "wait"
    elif phase == "wait":
        name = names(op["account"], op["share"], op["activeIndex"])["name"]
        result = gateway.search("GET", f"/indexers/{name}/status").get("lastResult") or {}
        if not result.get("startTime") or result["startTime"] == op.get("previousStart") or result.get("status") == "inProgress":
            return op
        if result.get("status") != "success" or result.get("itemsFailed"):
            raise StateConflict("The new source did not index successfully. Inspect diagnostics and resume.")
        mapping = parse_share_groups(gateway.download_settings().get("SHARE_GROUPS", ""))
        if mapping.get(op["sourceKey"]) != op["groupId"]:
            raise StateConflict("The download group changed during source connection.")
        gateway.indexer_action(name, "schedule" if op["interval"] else "pause", op["interval"])
        op.update(status="awaiting_access_verification", itemsProcessed=result.get("itemsProcessed"),
                  message="Source connected and indexing succeeded. Validate authorized and unauthorized users and account-qualified file links in the deployed agent.")
    else:
        raise StateConflict("The source connection contains an unknown phase.")
    op["updatedAt"] = now()
    return op
