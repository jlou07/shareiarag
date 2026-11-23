import copy
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from uuid import UUID

import requests
from azure.core.exceptions import AzureError, ClientAuthenticationError
from state import LeaseLost


class AzureApiError(RuntimeError):
    def __init__(self, message, status=502, http_status=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def editable(value):
    return {key: copy.deepcopy(item) for key, item in value.items() if not key.startswith("@odata.")}


def diagnostic(message):
    text = re.sub(r"https?://\S+", "[resource URL]", str(message))
    text = re.sub(r"(?i)(sig|token|key|secret|authorization)\s*[=:]\s*\S+", r"\1=[redacted]", text)
    return text[:500]


class AzureGateway:
    def __init__(self, settings, credential):
        self.settings = settings
        self.credential = credential
        self._local = threading.local()
        self._group_names = {}
        self._http_adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self._token_locks = {}
        self._token_locks_guard = threading.Lock()

    def bind_lease(self, lease):
        self._local.lease = lease

    def renew_lease(self):
        lease = getattr(self._local, "lease", None)
        if lease:
            try:
                lease.renew()
            except AzureError as error:
                raise LeaseLost("The maintenance lock was lost. This worker stopped before further Azure changes.") from error

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
            # Executor threads are short-lived; keep connection pools across their sessions.
            session.mount("https://", self._http_adapter)
        return session

    def _token(self, scope):
        with self._token_locks_guard:
            lock = self._token_locks.setdefault(scope, threading.Lock())
        # The credential caches tokens, but concurrent cache misses otherwise flood managed identity.
        with lock:
            return self.credential.get_token(scope).token

    def request(self, method, url, scope, body=None, headers=None, expected=(200,)):
        self.renew_lease()
        started = time.monotonic()
        try:
            token = self._token(scope)
            authenticated = time.monotonic()
            self.renew_lease()
            response = self._session().request(
                method, url, json=body,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                # Search validates data sources and skillsets on indexer writes; stay below the 60-second lease.
                timeout=(10, 45), allow_redirects=False,
            )
        except ClientAuthenticationError as error:
            logging.error("Azure managed identity authentication failed.")
            raise AzureApiError("Azure authentication failed. Check managed identity and RBAC.") from error
        except requests.RequestException as error:
            logging.error("Azure request failed: %s.", type(error).__name__)
            raise AzureApiError("Azure could not be reached. The operation outcome may require verification.") from error
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            target = urlsplit(url)
            logging.info(
                "Slow Azure call %s %s%s: token=%.3fs request=%.3fs status=%s",
                method, target.hostname, target.path, authenticated - started,
                time.monotonic() - authenticated, response.status_code,
            )
        self.renew_lease()
        if response.status_code not in expected:
            detail = ""
            try:
                error = response.json().get("error") or {}
                detail = diagnostic(error.get("message") or "") if isinstance(error, dict) else ""
            except (ValueError, AttributeError):
                pass
            logging.error("Azure API rejected %s request with HTTP %s: %s", method, response.status_code, detail or "no detail")
            raise AzureApiError(
                f"Azure returned HTTP {response.status_code}. "
                + (f"Azure said: {detail} " if detail else "")
                + "Check permissions, resource state and concurrent operations.",
                409 if response.status_code in (409, 412) else 502,
                response.status_code,
            )
        if response.status_code in (204, 404) or not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise AzureApiError("Azure returned an invalid JSON response.") from error

    def arm(self, method, path, body=None, version="2024-11-01", expected=(200,)):
        return self.request(
            method, f"https://management.azure.com{path}?api-version={version}",
            "https://management.azure.com/.default", body, expected=expected,
        )

    def search(self, method, path, body=None, headers=None, expected=(200,)):
        return self.request(
            method,
            f"https://{self.settings.search_name}.search.windows.net{path}?api-version={self.settings.search_version}",
            "https://search.azure.com/.default", body, headers, expected,
        )

    def sources(self, include_status=True):
        from source_connections import metadata

        with ThreadPoolExecutor(max_workers=8) as executor:
            requests_ = [executor.submit(self.search, "GET", path) for path in (
                f"/knowledgesources/{quote(self.settings.knowledge_source, safe='')}",
                "/indexers", "/skillsets", "/datasources")]
            source, indexers, skillsets, data_sources = [item.result() for item in requests_]
            active = source["searchIndexParameters"]["searchIndexName"]
            if active not in ("af-index-all", "af-index-all-b"):
                raise AzureApiError("The active index is outside the configured application slots.")
            indexers = [item for item in indexers["value"] if item.get("targetIndexName") in ("af-index-all", "af-index-all-b")]
            statuses = {
                item["name"]: executor.submit(self.search, "GET", f"/indexers/{quote(item['name'], safe='')}/status")
                for item in indexers
            } if include_status else {}
            statuses = {name: future.result() for name, future in statuses.items()}
        skill_by_name = {item["name"]: item for item in skillsets["value"]}
        data_by_name = {item["name"]: item for item in data_sources["value"]}
        rows = []
        for indexer in indexers:
            if indexer.get("targetIndexName") not in ("af-index-all", "af-index-all-b"):
                continue
            data = data_by_name.get(indexer.get("dataSourceName"), {})
            if data.get("type") != "azurefile":
                raise AzureApiError("A configured indexer has an unexpected source type.")
            skillset = skill_by_name.get(indexer.get("skillsetName"))
            if not skillset:
                raise AzureApiError("An indexer references a missing skillset.")
            group = group_from_skillset(skillset)
            status = statuses.get(indexer["name"], {})
            last = status.get("lastResult") or {}
            rows.append({
                "name": indexer["name"], "index": indexer["targetIndexName"],
                "active": indexer["targetIndexName"] == active,
                "source": indexer["dataSourceName"], "share": data.get("container", {}).get("name"),
                **metadata(self.settings, data),
                "skillset": indexer["skillsetName"], "groupId": group,
                "schedule": (indexer.get("schedule") or {}).get("interval"),
                "status": last.get("status", "neverRun"), "startTime": last.get("startTime"),
                "endTime": last.get("endTime"), "itemsProcessed": last.get("itemsProcessed"),
                "itemsFailed": last.get("itemsFailed"), "errorMessage": diagnostic(last.get("errorMessage") or ""),
                "errors": [diagnostic(item.get("errorMessage", item.get("message", ""))) for item in last.get("errors", [])[:10]],
                "warnings": [diagnostic(item.get("message", "")) for item in last.get("warnings", [])[:10]],
                "etag": indexer.get("@odata.etag"),
            })
        active_rows = [row for row in rows if row["active"]]
        if len({row["sourceKey"] for row in active_rows}) != len(active_rows):
            raise AzureApiError("Active source connections are missing or ambiguous; review the configuration.")
        return {"activeIndex": active, "indexers": rows, "knowledgeSource": source["name"]}

    def arm_collection(self, path, version="2023-05-01"):
        result = self.arm("GET", path, version=version)
        values = list(result["value"])
        while result.get("nextLink"):
            link = result["nextLink"]
            prefix = f"https://management.azure.com/subscriptions/{self.settings.subscription}/"
            if not link.lower().startswith(prefix.lower()):
                raise AzureApiError("Azure returned a collection link outside the configured subscription.")
            result = self.request("GET", link, "https://management.azure.com/.default")
            values.extend(result["value"])
        return values

    def source_names(self, sources):
        result = copy.deepcopy(sources)
        for group_id in {row["groupId"] for row in result["indexers"]}:
            cached = self._group_names.get(group_id)
            if cached and cached[0] > time.monotonic():
                name, error = cached[1:]
            else:
                name, error = None, None
                if not self.settings.directory_enabled:
                    error = "Group names unavailable: enable the approved Microsoft Graph directory lookup."
                else:
                    try:
                        group = self.request(
                            "GET", f"https://graph.microsoft.com/v1.0/groups/{group_id}?$select=id,displayName",
                            "https://graph.microsoft.com/.default",
                        )
                        if group.get("id", "").lower() != group_id or not group.get("displayName"):
                            raise AzureApiError("Microsoft Graph did not return the configured group's name.")
                        name = group["displayName"]
                    except AzureApiError as failure:
                        error = str(failure)
                self._group_names[group_id] = (time.monotonic() + (300 if name else 30), name, error)
            for row in result["indexers"]:
                if row["groupId"] == group_id:
                    row.update(groupName=name, groupNameError=error)
        return result

    def storage_accounts(self):
        values = self.arm_collection(f"/subscriptions/{self.settings.subscription}/providers/Microsoft.Storage/storageAccounts")
        accounts = []
        for value in values:
            parts = value["id"].split("/")
            if len(parts) != 9 or parts[2].lower() != self.settings.subscription.lower():
                raise AzureApiError("Azure returned an unexpected storage resource ID.")
            accounts.append({"account": value["name"], "resourceGroup": parts[4],
                             "subscriptionId": self.settings.subscription, "id": value["id"]})
        return {"subscriptionId": self.settings.subscription, "accounts": accounts}

    def storage_shares(self, resource_group, account):
        from source_connections import account_path

        path = account_path(self.settings, resource_group, account)
        values = self.arm_collection(path + "/fileServices/default/shares")
        return {"shares": [{"share": value["name"], "protocol": (value.get("properties") or {}).get("enabledProtocols", "SMB")}
                           for value in values]}

    def verify_storage_share(self, resource_group, account, share):
        from source_connections import account_path, validate_share

        path = account_path(self.settings, resource_group, account)
        validate_share(share)
        value = self.arm("GET", path + f"/fileServices/default/shares/{share}", version="2023-05-01")
        if (value.get("properties") or {}).get("enabledProtocols", "SMB") != "SMB":
            raise ValueError("Only SMB shares are supported by this application.")

    def publish_source_download(self, account, share, group):
        from source_connections import SourcePrerequisiteError, connection, secret_name, source_identity

        connection(account)
        if not self.settings.key_vault_name:
            raise SourcePrerequisiteError("key_vault_not_configured", "Configure KEY_VAULT_NAME before connecting a source.", "KEY_VAULT_NAME")
        key = f"@Microsoft.KeyVault(VaultName={self.settings.key_vault_name};SecretName={secret_name(account)})"
        settings = self.download_settings()
        if settings.get("MULTI_ACCOUNT_SOURCES_VERSION") != "1":
            raise ValueError("Deploy the multi-account download Function and set MULTI_ACCOUNT_SOURCES_VERSION=1 before connecting a source.")
        mapping = parse_share_groups(settings.get("SHARE_GROUPS", ""))
        source_key = source_identity(account, share)
        if mapping.get(source_key) not in (None, group):
            raise AzureApiError("This download source already has a different group.", 409)
        mapping[source_key] = group
        settings[f"FILES_ACCOUNT_KEY_{account.upper()}"] = key
        settings["SHARE_GROUPS"] = ";".join(f"{name}={value}" for name, value in sorted(mapping.items()))
        self.arm("PUT", self.settings.resource_id("Microsoft.Web/sites", self.settings.download_app) + "/config/appsettings",
                 {"properties": settings})
        verified = self.download_settings()
        if parse_share_groups(verified.get("SHARE_GROUPS", "")).get(source_key) != group or verified.get(f"FILES_ACCOUNT_KEY_{account.upper()}") != key:
            raise AzureApiError("The source download configuration could not be verified.", 409)

    def remove_source(self, source_key):
        sources = self.sources(include_status=False)
        rows = [row for row in sources["indexers"] if row["sourceKey"] == source_key]
        if not rows:
            raise AzureApiError("This source is not connected.", 404)
        template = None
        if not any(row["active"] and row["sourceKey"] != source_key for row in sources["indexers"]):
            # Keep the last configuration so a share can be connected again once all sources are gone.
            active = next(row for row in rows if row["active"])
            template = {
                "skillset": editable(self.search("GET", f"/skillsets/{quote(active['skillset'], safe='')}")),
                "indexer": editable(self.search("GET", f"/indexers/{quote(active['name'], safe='')}")),
            }
        for row in rows:
            self.search("DELETE", f"/indexers/{quote(row['name'], safe='')}", expected=(200, 204, 404))
        for skillset in {row["skillset"] for row in rows}:
            if not any(item["skillset"] == skillset and item["sourceKey"] != source_key for item in sources["indexers"]):
                self.search("DELETE", f"/skillsets/{quote(skillset, safe='')}", expected=(200, 204, 404))
        for data_source in {row["source"] for row in rows}:
            if not any(item["source"] == data_source and item["sourceKey"] != source_key for item in sources["indexers"]):
                self.search("DELETE", f"/datasources/{quote(data_source, safe='')}", expected=(200, 204, 404))
        settings = self.download_settings()
        mapping = parse_share_groups(settings.get("SHARE_GROUPS", ""))
        if source_key in mapping:
            del mapping[source_key]
            settings["SHARE_GROUPS"] = ";".join(f"{name}={value}" for name, value in sorted(mapping.items()))
            account = rows[0]["account"]
            if account != self.settings.files_account and not any(name.startswith(f"{account}/") for name in mapping):
                settings.pop(f"FILES_ACCOUNT_KEY_{account.upper()}", None)
            self.arm("PUT", self.settings.resource_id("Microsoft.Web/sites", self.settings.download_app) + "/config/appsettings",
                     {"properties": settings})
            if source_key in parse_share_groups(self.download_settings().get("SHARE_GROUPS", "")):
                raise AzureApiError("The source download configuration could not be removed.", 409)
        return {"status": "completed", "removed": sorted(row["name"] for row in rows), "template": template}

    def resource(self, provider, name, version="2024-11-01"):
        value = self.arm("GET", self.settings.resource_id(provider, name), version=version)
        props = value.get("properties") or {}
        return {
            "name": value["name"], "id": value["id"], "type": value["type"],
            "location": value.get("location"), "state": props.get("state", props.get("provisioningState")),
            "sku": (value.get("sku") or {}).get("name"),
            "portal": f"https://portal.azure.com/#resource{value['id']}",
        }

    def overview(self):
        tasks = {
            "sources": lambda: self.source_names(self.sources()),
            "files": lambda: self.resource("Microsoft.Storage/storageAccounts", self.settings.files_account, "2023-05-01"),
            "search": lambda: self.resource("Microsoft.Search/searchServices", self.settings.search_name, "2023-11-01"),
            "foundry": lambda: self.resource("Microsoft.CognitiveServices/accounts", self.settings.foundry_account, "2024-10-01"),
            "downloads": lambda: self.resource("Microsoft.Web/sites", self.settings.download_app),
            "hosting": lambda: self.arm("GET", self.settings.hosting_plan_id),
        }
        def load_section(name, load):
            try:
                value = load()
                if name == "hosting":
                    value = {
                        "name": value["name"], "sku": value["sku"]["name"],
                        "location": value["location"], "id": value["id"],
                    }
                return {"status": "ok", "data": value}
            except AzureApiError as error:
                return {"status": "error", "error": str(error)}

        with ThreadPoolExecutor(max_workers=len(tasks) + 1) as executor:
            pending = {name: executor.submit(load_section, name, load) for name, load in tasks.items()}
            sources = pending["sources"].result()
            if sources["status"] == "ok":
                active = sources["data"]["activeIndex"]
                pending["index"] = executor.submit(
                    load_section, "index", lambda: self.search("GET", f"/indexes/{active}/stats"))
            sections = {name: future.result() for name, future in pending.items()}
        return {"generatedAt": datetime.now(timezone.utc).isoformat(), "sections": sections}

    def indexer_action(self, name, action, interval=None):
        sources = self.sources()
        row = next((item for item in sources["indexers"] if item["name"] == name), None)
        if not row or not row["active"]:
            raise AzureApiError("Only current active indexers can be controlled.", 409)
        path = f"/indexers/{quote(name, safe='')}"
        if action == "run":
            self.search("POST", path + "/run", expected=(202,))
            return {"status": "accepted", "message": "Incremental indexing accepted; see live execution status."}
        if action not in ("pause", "schedule"):
            raise ValueError("Unsupported indexer action.")
        if action == "schedule" and interval not in ("PT5M", "PT15M", "PT30M", "PT1H"):
            raise ValueError("Choose a supported indexing interval.")
        current = self.search("GET", path)
        body = editable(current)
        if action == "pause":
            body.pop("schedule", None)
        else:
            body["schedule"] = {"interval": interval}
        self.search("PUT", path, body, {"If-Match": current["@odata.etag"]}, (200, 201, 204))
        verified = self.search("GET", path)
        actual = (verified.get("schedule") or {}).get("interval")
        expected = None if action == "pause" else interval
        if actual != expected:
            raise AzureApiError("The indexing schedule update could not be verified.", 409)
        return {"status": "completed", "schedule": actual}

    def metrics(self, hours):
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        targets = {
            "search": (self.settings.resource_id("Microsoft.Search/searchServices", self.settings.search_name),
                       ["SearchLatency", "ThrottledSearchQueriesPercentage", "DocumentsProcessedCount"]),
            "foundry": (self.settings.resource_id("Microsoft.CognitiveServices/accounts", self.settings.foundry_account),
                        ["ModelRequests", "TimeToResponse", "TotalTokens", "FoundryModelEstimatedCost"]),
            "hosting": (self.settings.hosting_plan_id, ["CpuPercentage", "MemoryPercentage"]),
        }
        output = {}
        timespan = quote(
            f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}", safe="",
        )
        for key, (resource, names) in targets.items():
            try:
                definitions = self.arm(
                    "GET", resource + "/providers/Microsoft.Insights/metricDefinitions", version="2018-01-01",
                )["value"]
                available = {item["name"]["value"]: item for item in definitions}
                series = []
                for name in names:
                    if name not in available:
                        series.append({"name": name, "status": "unavailable", "reason": "Metric not exposed by this resource."})
                        continue
                    definition = available[name]
                    aggregation = definition.get("primaryAggregationType", "Average")
                    supported = definition.get("supportedAggregationTypes") or [aggregation]
                    if aggregation not in supported:
                        aggregation = supported[0]
                    url = (
                        f"https://management.azure.com{resource}/providers/Microsoft.Insights/metrics"
                        f"?api-version=2018-01-01&metricnames={quote(name)}&timespan={timespan}"
                        f"&interval={'PT1H' if hours > 24 else 'PT5M'}&aggregation={aggregation}"
                    )
                    result = self.request("GET", url, "https://management.azure.com/.default")
                    values = [
                        {"time": point["timeStamp"], "value": point.get(aggregation.lower())}
                        for item in result.get("value", [])
                        for timeline in item.get("timeseries", []) for point in timeline.get("data", [])
                    ]
                    series.append({
                        "name": name, "unit": definition.get("unit"), "aggregation": aggregation,
                        "status": "ok" if any(p["value"] is not None for p in values) else "no_data",
                        "points": values,
                    })
                    if key == "foundry" and name == "ModelRequests":
                        limited = self.request(
                            "GET", url + "&$filter=StatusCode%20eq%20%27429%27",
                            "https://management.azure.com/.default",
                        )
                        limited_points = [
                            {"time": point["timeStamp"], "value": point.get(aggregation.lower())}
                            for item in limited.get("value", []) for timeline in item.get("timeseries", [])
                            for point in timeline.get("data", [])
                        ]
                        series.append({
                            "name": "ModelRequests429", "unit": definition.get("unit"), "aggregation": aggregation,
                            "status": "ok" if any(p["value"] is not None for p in limited_points) else "no_data",
                            "points": limited_points,
                        })
                output[key] = {"status": "ok", "metrics": series}
            except AzureApiError as error:
                output[key] = {"status": "error", "error": str(error)}
        try:
            output["downloads"] = {"status": "ok", "metrics": self.download_metrics(hours)}
        except AzureApiError as error:
            output["downloads"] = {"status": "error", "error": str(error)}
        return {"hours": hours, "start": start.isoformat(), "end": end.isoformat(), "resources": output}

    def download_metrics(self, hours):
        if hours not in (1, 6, 24, 168):
            raise ValueError("Choose a supported metrics time range.")
        component = self.arm(
            "GET", self.settings.resource_id("Microsoft.Insights/components", self.settings.download_app),
            version="2020-02-02",
        )
        app_id = component.get("properties", {}).get("AppId")
        if not app_id or not re.fullmatch(r"[a-fA-F0-9-]{36}", app_id):
            raise AzureApiError("The file-opening Application Insights application ID is unavailable.")
        interval = "1h" if hours > 24 else "5m"
        result = self.request(
            "POST", f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
            "https://api.applicationinsights.io/.default",
            {"query": (
                f"requests | where timestamp > ago({hours}h) "
                "| summarize Requests=count(), Http5xx=countif(toint(resultCode) >= 500), "
                "DurationMs=avg(duration), P95DurationMs=percentile(duration, 95) "
                f"by bin(timestamp, {interval}) | order by timestamp asc"
            )},
        )
        tables = result.get("tables")
        if not isinstance(tables, list) or not tables:
            raise AzureApiError("Application Insights returned no result table.")
        table = tables[0]
        columns = {column["name"]: index for index, column in enumerate(table["columns"])}
        expected = ("timestamp", "Requests", "Http5xx", "DurationMs", "P95DurationMs")
        if any(name not in columns for name in expected):
            raise AzureApiError("Application Insights returned an unexpected metrics structure.")
        return [{
            "name": name, "unit": "Count" if name in ("Requests", "Http5xx") else "Milliseconds",
            "aggregation": "Count" if name in ("Requests", "Http5xx") else ("P95" if name == "P95DurationMs" else "Average"),
            "status": "ok" if table["rows"] else "no_data",
            "points": [{"time": row[columns["timestamp"]], "value": row[columns[name]]} for row in table["rows"]],
        } for name in expected[1:]]

    def groups(self, search="", cursor=""):
        if not isinstance(search, str) or len(search) > 256 or any(ord(char) < 32 for char in search):
            raise ValueError("The group search must be at most 256 characters without control characters.")
        if not isinstance(cursor, str) or len(cursor) > 8192 or any(ord(char) < 32 for char in cursor):
            raise ValueError("The group continuation token is invalid.")
        search = search.strip()
        if not self.settings.directory_enabled:
            if cursor:
                raise ValueError("Directory lookup is not enabled.")
            ids = sorted({row["groupId"] for row in self.sources()["indexers"]})
            return {
                "directoryStatus": "not_configured",
                "message": "Directory lookup is not enabled. Existing configured group IDs remain selectable; no group names or membership are inferred.",
                "groups": [{"id": group, "displayName": group} for group in ids if search.casefold() in group.casefold()],
                "moreAvailable": False, "nextCursor": None,
            }

        try:
            group_id = str(UUID(search)) if search else None
        except ValueError:
            group_id = None

        scope = "https://graph.microsoft.com/.default"
        if group_id:
            if cursor:
                raise ValueError("A group ID search cannot have a continuation token.")
            group = self.request(
                "GET", f"https://graph.microsoft.com/v1.0/groups/{group_id}?$select=id,displayName,securityEnabled",
                scope, expected=(200, 404),
            )
            result = {"value": [group] if group is not None else []}
        else:
            group_filter = "securityEnabled eq true"
            if search:
                escaped = search.replace("'", "''")
                group_filter += f" and startswith(displayName,'{escaped}')"
            params = {"$select": "id,displayName,securityEnabled", "$filter": group_filter, "$top": 50}
            if cursor:
                params["$skiptoken"] = cursor
            result = self.request("GET", f"https://graph.microsoft.com/v1.0/groups?{urlencode(params)}", scope)

        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise AzureApiError("Microsoft Graph returned an unexpected group collection.")
        groups = []
        for item in result["value"]:
            if not isinstance(item, dict) or not isinstance(item.get("securityEnabled"), bool):
                raise AzureApiError("Microsoft Graph returned incomplete group metadata.")
            if not item["securityEnabled"]:
                continue
            if not isinstance(item.get("id"), str) or not isinstance(item.get("displayName"), str) or not item["displayName"]:
                raise AzureApiError("Microsoft Graph returned incomplete group metadata.")
            try:
                item_id = str(UUID(item["id"]))
            except ValueError as error:
                raise AzureApiError("Microsoft Graph returned an invalid group ID.") from error
            groups.append({"id": item_id, "displayName": item["displayName"]})

        next_cursor = None
        if "@odata.nextLink" in result:
            next_link = result["@odata.nextLink"]
            if not isinstance(next_link, str) or not next_link:
                raise AzureApiError("Microsoft Graph returned an invalid group continuation link.")
            try:
                link = urlsplit(next_link)
            except ValueError as error:
                raise AzureApiError("Microsoft Graph returned an invalid group continuation link.") from error
            tokens = parse_qs(link.query).get("$skiptoken", [])
            if link.scheme != "https" or link.netloc.lower() != "graph.microsoft.com" or link.path != "/v1.0/groups" or link.fragment or len(tokens) != 1:
                raise AzureApiError("Microsoft Graph returned an invalid group continuation link.")
            next_cursor = tokens[0]
            if len(next_cursor) > 8192 or any(ord(char) < 32 for char in next_cursor):
                raise AzureApiError("Microsoft Graph returned an invalid group continuation token.")
        return {
            "directoryStatus": "ok", "moreAvailable": bool(next_cursor), "nextCursor": next_cursor,
            "groups": groups,
        }

    def verify_group(self, group_id):
        if group_id in {row["groupId"] for row in self.sources()["indexers"]}:
            return
        if not self.settings.directory_enabled:
            raise ValueError("This group is not currently configured. Enable read-only directory verification before associating another group.")
        group = self.request(
            "GET", f"https://graph.microsoft.com/v1.0/groups/{group_id}?$select=id,securityEnabled",
            "https://graph.microsoft.com/.default",
        )
        if not group.get("securityEnabled"):
            raise ValueError("A security-enabled Entra group is required.")

    def download_settings(self):
        return self.arm(
            "POST", self.settings.resource_id("Microsoft.Web/sites", self.settings.download_app) + "/config/appsettings/list",
        )["properties"]

    def set_download_group(self, share, group_id, allowed_current):
        settings = self.download_settings()
        mapping = parse_share_groups(settings.get("SHARE_GROUPS", ""))
        if mapping.get(share) not in allowed_current:
            raise AzureApiError("The download group was changed outside this operation.", 409)
        if group_id is None:
            mapping.pop(share, None)
        else:
            mapping[share] = group_id
        settings["SHARE_GROUPS"] = ";".join(f"{key}={value}" for key, value in sorted(mapping.items()))
        self.arm(
            "PUT", self.settings.resource_id("Microsoft.Web/sites", self.settings.download_app) + "/config/appsettings",
            {"properties": settings},
        )
        check = parse_share_groups(self.download_settings().get("SHARE_GROUPS", ""))
        if check.get(share) != group_id:
            raise AzureApiError("The download permission update could not be verified.", 409)


def group_from_skillset(skillset):
    group_skills = [skill for skill in skillset.get("skills", []) if skill.get("name") == "group-id"]
    if len(group_skills) != 1 or not any(
        item.get("name") == "condition" and item.get("source", "").replace(" ", "") == "=true"
        for item in group_skills[0].get("inputs", [])
    ):
        raise AzureApiError("The group skill must use the reviewed unconditional source-group mapping.")
    selectors = skillset.get("indexProjections", {}).get("selectors", [])
    if not selectors or any(
        not any(item.get("name") == "group_ids" and item.get("source") == "/document/group_ids" for item in selector.get("mappings", []))
        for selector in selectors
    ):
        raise AzureApiError("A source skillset does not project the expected permissions onto each chunk.")
    matches = [
        item.get("source") for skill in skillset.get("skills", []) if skill.get("name") == "group-id"
        for item in skill.get("inputs", []) if item.get("name") == "whenTrue"
    ]
    if len(matches) != 1:
        raise AzureApiError("The source group skill is missing or ambiguous.")
    match = re.fullmatch(r"=\s*'([0-9a-fA-F-]{36})'", matches[0] or "")
    if not match:
        raise AzureApiError("The configured source group is not a literal Entra group identifier.")
    return match.group(1).lower()


def parse_share_groups(value):
    pairs = [part.split("=", 1) for part in value.split(";") if part]
    if any(len(pair) != 2 for pair in pairs):
        raise AzureApiError("The download Function group mapping is malformed.")
    mapping = dict(pairs)
    if len(mapping) != len(pairs):
        raise AzureApiError("The download Function contains duplicate share mappings.")
    return mapping
