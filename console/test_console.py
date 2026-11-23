import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from uuid import uuid4

import jwt
from azure.core.exceptions import HttpResponseError

from auth import AuthenticationError, AuthorizationError, Principal, TokenVerifier
from azure_gateway import AzureApiError, AzureGateway, group_from_skillset, parse_share_groups
import index_rebuild
import source_connections
from operations import Controller, TERMINAL, Worker, sanitized_schema
from settings import Settings, identifier
from state import BlobState, MemoryState, StateConflict, StateError
from web import create_app


ADMIN = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
GROUP_A = "44444444-4444-4444-8444-444444444444"
GROUP_B = "55555555-5555-4555-8555-555555555555"
SUB = "66666666-6666-4666-8666-666666666666"
SETTINGS = Settings(
    subscription=SUB, tenant=TENANT, client_id=CLIENT, administrators=frozenset({ADMIN}),
    hosting_plan_id=f"/subscriptions/{SUB}/resourceGroups/hosting/providers/Microsoft.Web/serverfarms/test-plan",
    key_vault_name="kv-test-vault",
)


def skill(name, group, index):
    return {
        "name": name, "@odata.etag": '"v1"',
        "skills": [{"name": "group-id", "inputs": [
            {"name": "condition", "source": "= true"},
            {"name": "whenTrue", "source": f"= '{group}'"},
        ]}, {"name": "share-name", "inputs": [{"name": "whenTrue", "source": "= 'template-share'"}]}],
        "indexProjections": {"selectors": [{"targetIndexName": index, "mappings": [
            {"name": "group_ids", "source": "/document/group_ids"},
        ]}]},
    }


def schema(name):
    return {
        "name": name, "@odata.etag": '"v1"', "permissionFilterOption": "enabled",
        "fields": [{"name": "group_ids", "permissionFilter": "groupIds"},
                   {"name": "chunk_id", "sortable": True, "filterable": True},
                   {"name": "metadata_storage_path"}],
        "vectorSearch": {"vectorizers": [{"azureOpenAIParameters": {"apiKey": "never-persist-this-secret"}}]},
    }


class FakeGateway(AzureGateway):
    def __init__(self):
        super().__init__(SETTINGS, Mock())
        self.active = "af-index-all-b"
        self.indexes = {name: schema(name) for name in ("af-index-all", "af-index-all-b")}
        self.indexers = {}
        self.skillsets = {}
        self.statuses = {}
        self.data_sources = {
            f"af-ds-{name}": {"name": f"af-ds-{name}", "type": "azurefile",
                             "container": {"name": f"{name}-share"}, "credentials": {"connectionString": "private-connection"}}
            for name in ("admin", "alluser")
        }
        self.mapping = {"admin-share": GROUP_A, "alluser-share": GROUP_B}
        self.calls = []
        self.run_number = 0
        self.fail_run = False
        self.counts = {}
        self.documents = {}
        self.deleted = []
        self.deny_inventory = None
        self.deny_delete = False
        for suffix, index in (("", "af-index-all"), ("-b", "af-index-all-b")):
            for share in ("admin", "alluser"):
                name = f"af-indexer-all{suffix}-{share}"
                skill_name = f"af-skills-all{suffix}-{share}"
                self.indexers[name] = {
                    "name": name, "@odata.etag": '"v1"', "targetIndexName": index,
                    "skillsetName": skill_name, "dataSourceName": f"af-ds-{share}",
                }
                if suffix:
                    self.indexers[name]["schedule"] = {"interval": "PT5M"}
                self.skillsets[skill_name] = skill(skill_name, GROUP_A if share == "admin" else GROUP_B, index)
                self.statuses[name] = {"lastResult": {
                    "status": "success", "startTime": "2026-01-01T00:00:00Z",
                    "itemsProcessed": 2, "itemsFailed": 0, "warnings": [], "errors": [],
                }}

    def sources(self, include_status=True):
        return super().sources(include_status=include_status)

    def search(self, method, path, body=None, headers=None, expected=(200,)):
        self.calls.append((method, path))
        parts = path.strip("/").split("/")
        if len(parts) == 1 and method == "GET":
            return {"value": copy.deepcopy(list({"indexers": self.indexers, "skillsets": self.skillsets,
                                                "datasources": self.data_sources}[parts[0]].values()))}
        kind, name = parts[:2]
        if kind == "indexers":
            if len(parts) == 3:
                action = parts[2]
                if action == "status":
                    return copy.deepcopy(self.statuses[name])
                if action == "run":
                    if self.fail_run:
                        raise AzureApiError("Simulated indexing failure.")
                    if self.indexers[name].get("disabled"):
                        raise AzureApiError("A disabled indexer cannot be run.")
                    self.run_number += 1
                    self.statuses[name]["lastResult"]["startTime"] = f"2026-01-02T00:00:{self.run_number:02d}Z"
                    self.statuses[name]["lastResult"].setdefault("status", "success")
                    self.statuses[name]["lastResult"].setdefault("itemsFailed", 0)
                    return None
                if action == "reset":
                    return None
            collection = self.indexers
        elif kind == "skillsets":
            collection = self.skillsets
        elif kind == "datasources":
            collection = self.data_sources
        elif kind == "indexes":
            if len(parts) == 4 and parts[2:] == ["docs", "search"]:
                self.assert_inventory_request(body, headers)
                if self.deny_inventory:
                    raise AzureApiError("Rejected.", http_status=self.deny_inventory)
                documents = sorted(self.documents.get(name, []), key=lambda item: item["chunk_id"])
                if body.get("filter"):
                    after = body["filter"].split(" gt ", 1)[1][1:-1].replace("''", "'")
                    documents = [item for item in documents if item["chunk_id"] > after]
                return {"value": copy.deepcopy(documents[:body["top"]])}
            if len(parts) == 4 and parts[2:] == ["docs", "index"]:
                if self.deny_delete:
                    raise AzureApiError("Forbidden.", http_status=403)
                keys = [item["chunk_id"] for item in body["value"] if item["@search.action"] == "delete"]
                self.deleted.extend(keys)
                self.documents[name] = [item for item in self.documents.get(name, []) if item["chunk_id"] not in keys]
                return {"value": [{"key": key, "status": True, "statusCode": 200} for key in keys]}
            if len(parts) == 3 and parts[2] == "stats":
                return {"documentCount": self.counts.get(name, 4)}
            collection = self.indexes
        elif kind == "knowledgesources":
            if method == "GET":
                return {"name": name, "@odata.etag": f'"{self.active}"', "kind": "searchIndex",
                        "searchIndexParameters": {"searchIndexName": self.active}}
            if method == "PUT":
                if headers.get("If-Match") != f'"{self.active}"':
                    raise AzureApiError("Precondition failed.", 412)
                self.active = body["searchIndexParameters"]["searchIndexName"]
                return None
            raise AssertionError(f"Unexpected {method} {path}")

        else:
            raise AssertionError(f"Unexpected {method} {path}")
        if method == "GET":
            if name not in collection:
                if 404 in expected:
                    return None
                raise AzureApiError("Missing test resource.")
            return copy.deepcopy(collection[name])
        if method == "DELETE":
            collection.pop(name, None)
            return None
        if method == "PUT":
            collection[name] = {**copy.deepcopy(body), "@odata.etag": '"v2"'}
            if kind == "indexers":
                self.statuses.setdefault(name, {"lastResult": {}})
            return copy.deepcopy(collection[name])
        raise AssertionError(f"Unexpected {method} {path}")

    def assert_inventory_request(self, body, headers):
        if body["select"] != "chunk_id,metadata_storage_path":
            raise AssertionError("Inventory must request only file metadata.")
        if headers != {"x-ms-enable-elevated-read": "true"}:
            raise AssertionError("Protected inventory requires an elevated read without a user token.")

    def download_settings(self):
        return {"SHARE_GROUPS": ";".join(f"{key}={value}" for key, value in self.mapping.items()), "FILES_ACCOUNT_KEY": "not-for-browser"}

    def set_download_group(self, share, group_id, allowed_current):
        if self.mapping.get(share) not in allowed_current:
            raise StateConflict("Concurrent download configuration change.")
        if group_id is None:
            self.mapping.pop(share, None)
        else:
            self.mapping[share] = group_id

    def overview(self):
        return {"sections": {"sources": {"status": "ok", "data": self.sources()}}}

    def groups(self, search="", cursor=""):
        return {"directoryStatus": "not_configured", "groups": [{"id": GROUP_A}, {"id": GROUP_B}]}


class SettingsAndParsingTests(unittest.TestCase):
    def test_invalid_identifier_is_a_validation_error(self):
        for value in (None, 42, "", "../index"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                identifier(value)

    def test_group_skill_requires_unconditional_mapping(self):
        value = skill("test", GROUP_A, "index")
        value["skills"][0]["inputs"][0]["source"] = "= false"
        with self.assertRaises(AzureApiError):
            group_from_skillset(value)

    def test_group_skill_requires_chunk_projection(self):
        value = skill("test", GROUP_A, "index")
        value["indexProjections"]["selectors"][0]["mappings"] = []
        with self.assertRaises(AzureApiError):
            group_from_skillset(value)

    def test_group_skill_returns_actual_guid(self):
        self.assertEqual(group_from_skillset(skill("test", GROUP_A, "index")), GROUP_A)

    def test_duplicate_download_mapping_is_rejected(self):
        with self.assertRaises(AzureApiError):
            parse_share_groups("share=a;share=b")

    def test_malformed_download_mapping_is_rejected(self):
        with self.assertRaises(AzureApiError):
            parse_share_groups("share")

    def test_schema_removes_secrets_without_mutating_original(self):
        original = schema("index")
        cleaned = sanitized_schema(original)
        self.assertNotIn("never-persist-this-secret", json.dumps(cleaned))
        self.assertIn("apiKey", original["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"])

    def test_unprotected_schema_is_rejected(self):
        value = schema("index")
        value["permissionFilterOption"] = "disabled"
        with self.assertRaises(ValueError):
            sanitized_schema(value)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.verifier = TokenVerifier(SETTINGS)
        self.verifier.keys = Mock()
        self.claims = {"tid": TENANT, "oid": ADMIN, "scp": "Console.Access", "name": "Test Admin"}

    def test_backend_checks_issuer_audience_and_required_claims(self):
        with patch("auth.jwt.decode", return_value=self.claims) as decode:
            principal = self.verifier.verify("token")
        self.assertEqual(principal.object_id, ADMIN)
        self.assertEqual(decode.call_args.kwargs["audience"], CLIENT)
        self.assertEqual(decode.call_args.kwargs["issuer"], f"https://login.microsoftonline.com/{TENANT}/v2.0")
        self.assertIn("exp", decode.call_args.kwargs["options"]["require"])
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])

    def test_wrong_tenant_is_denied(self):
        with patch("auth.jwt.decode", return_value={**self.claims, "tid": CLIENT}):
            with self.assertRaises(AuthorizationError):
                self.verifier.verify("token")

    def test_document_group_membership_does_not_grant_console_access(self):
        with patch("auth.jwt.decode", return_value={**self.claims, "oid": GROUP_A, "groups": [GROUP_A]}):
            with self.assertRaises(AuthorizationError):
                self.verifier.verify("token")

    def test_id_token_without_delegated_scope_is_denied(self):
        with patch("auth.jwt.decode", return_value={**self.claims, "scp": ""}):
            with self.assertRaises(AuthorizationError):
                self.verifier.verify("token")

    def test_invalid_signature_is_denied(self):
        with patch("auth.jwt.decode", side_effect=jwt.InvalidSignatureError):
            with self.assertRaises(AuthenticationError):
                self.verifier.verify("token")

    def test_search_token_requires_matching_administrator_scope_and_client(self):
        claims = {**self.claims, "scp": "user_impersonation", "appid": CLIENT}
        principal = Principal(ADMIN, "Test Admin")
        with patch("auth.jwt.decode", return_value=claims) as decode:
            self.verifier.verify_search("search-token", principal)
        self.assertIn("https://search.azure.com", decode.call_args.kwargs["audience"])
        self.assertIn(f"https://sts.windows.net/{TENANT}/", decode.call_args.kwargs["issuer"])
        for override in ({"oid": GROUP_A}, {"tid": CLIENT}, {"scp": ""},
                         {"appid": GROUP_A}, {"azp": GROUP_A}):
            with self.subTest(override=override), patch("auth.jwt.decode", return_value={**claims, **override}):
                with self.assertRaises(AuthorizationError):
                    self.verifier.verify_search("search-token", principal)

    def test_search_token_signature_and_expiry_are_verified(self):
        with patch("auth.jwt.decode", side_effect=jwt.ExpiredSignatureError):
            with self.assertRaises(AuthenticationError):
                self.verifier.verify_search("expired", Principal(ADMIN, "Test Admin"))

    def test_real_signed_access_token_is_verified_and_expired_token_rejected(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.verifier.keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
        moment = datetime.now(timezone.utc)
        claims = {
            **self.claims, "aud": CLIENT, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "iat": moment, "nbf": moment, "exp": moment + timedelta(minutes=5),
        }
        encoded = jwt.encode(claims, key, algorithm="RS256")
        self.assertEqual(self.verifier.verify(encoded).object_id, ADMIN)
        expired = jwt.encode({**claims, "exp": moment - timedelta(minutes=1)}, key, algorithm="RS256")
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(expired)
        search_claims = {
            **claims, "aud": "https://search.azure.com", "iss": f"https://sts.windows.net/{TENANT}/",
            "scp": "user_impersonation", "appid": CLIENT,
        }
        search_token = jwt.encode(search_claims, key, algorithm="RS256")
        self.verifier.verify_search(search_token, Principal(ADMIN, "Test Admin"))
        for override in ({"aud": CLIENT}, {"iss": "https://sts.windows.net/another-tenant/"},
                         {"exp": moment - timedelta(minutes=1)}):
            with self.subTest(override=override):
                invalid = jwt.encode({**search_claims, **override}, key, algorithm="RS256")
                with self.assertRaises(AuthenticationError):
                    self.verifier.verify_search(invalid, Principal(ADMIN, "Test Admin"))


class MetricsTests(unittest.TestCase):
    def test_metrics_use_utc_z_not_an_ambiguous_plus_offset(self):
        gateway = AzureGateway(SETTINGS, Mock())
        gateway.arm = Mock(return_value={"value": [{
            "name": {"value": "CpuPercentage"}, "primaryAggregationType": "Average",
            "supportedAggregationTypes": ["Average"], "unit": "Percent",
        }]})
        gateway.request = Mock(return_value={"value": [{"timeseries": [{"data": [
            {"timeStamp": "2026-01-01T00:00:00Z", "average": None},
        ]}]}]})
        gateway.download_metrics = Mock(return_value=[])
        result = gateway.metrics(1)
        urls = [call.args[1] for call in gateway.request.call_args_list]
        self.assertTrue(urls)
        self.assertTrue(all("%2B00" not in url and "%20" not in url.split("timespan=")[1].split("&")[0] for url in urls))
        self.assertTrue(all("Z%2F" in url for url in urls))
        self.assertEqual(result["resources"]["hosting"]["metrics"][0]["status"], "no_data")

    def test_application_insights_query_returns_only_aggregates(self):
        gateway = AzureGateway(SETTINGS, Mock())
        gateway.arm = Mock(return_value={"properties": {"AppId": CLIENT}})
        gateway.request = Mock(return_value={"tables": [{
            "columns": [{"name": value} for value in ("timestamp", "Requests", "Http5xx", "DurationMs", "P95DurationMs")],
            "rows": [["2026-01-01T00:00:00Z", 5, 0, 100, 150]],
        }]})
        metrics = gateway.download_metrics(24)
        body = gateway.request.call_args.args[3]["query"]
        self.assertNotIn("url", body.lower())
        self.assertNotIn("/ 1ms", body)
        self.assertEqual(metrics[2]["unit"], "Milliseconds")
        self.assertEqual(metrics[1]["points"][0]["value"], 0)


class PersistenceTests(unittest.TestCase):
    def test_invalid_state_never_reaches_a_mutation(self):
        state = BlobState.__new__(BlobState)
        state.initialized = True
        state.blob = Mock()
        change = Mock()
        for invalid in ([], {"version": 1}, {"version": 1, "operations": {}, "drafts": [], "audit": []}):
            with self.subTest(invalid=invalid):
                state.blob.download_blob.return_value.readall.return_value = json.dumps(invalid)
                with self.assertRaises(StateError):
                    state.read()
                with self.assertRaises(StateError):
                    state.update(change)
        change.assert_not_called()
        state.blob.upload_blob.assert_not_called()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        self.verifier = Mock()
        self.verifier.verify.return_value = Principal(ADMIN, "Test Admin")
        self.app = create_app(SETTINGS, self.gateway, self.store, self.verifier, start_worker=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer test", "Origin": "http://localhost"}

    def test_health_is_public_without_starting_maintenance(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.verifier.verify.assert_not_called()
        self.assertFalse(self.app.extensions["worker"].alive)

    def test_public_configuration_contains_no_administrator_or_azure_keys(self):
        response = self.client.get("/public-config")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn(ADMIN, body)
        self.assertNotIn("ACCOUNT_KEY", body)

    def test_unsigned_easy_auth_headers_do_not_authenticate(self):
        response = self.client.get("/api/sources", headers={"x-ms-client-principal": "forged"})
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_api_is_denied(self):
        self.assertEqual(self.client.get("/api/overview").status_code, 401)

    def test_authorized_sources_discover_slot_b(self):
        response = self.client.get("/api/sources", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["activeIndex"], "af-index-all-b")
        self.assertNotIn("FILES_ACCOUNT_KEY", response.get_data(as_text=True))

    def test_group_search_requires_auth_and_forwards_search_and_cursor(self):
        self.gateway.groups = Mock(return_value={"directoryStatus": "ok", "groups": []})
        self.assertEqual(self.client.get("/api/groups?search=Team").status_code, 401)
        response = self.client.get("/api/groups?search=Team&cursor=next", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.gateway.groups.assert_called_once_with("Team", "next")

    def test_backend_rejects_non_administrator(self):
        self.verifier.verify.side_effect = AuthorizationError("Not an administrator.")
        self.assertEqual(self.client.get("/api/sources", headers=self.headers).status_code, 403)

    def test_cross_origin_mutation_is_denied(self):
        headers = {**self.headers, "Origin": "https://attacker.example"}
        response = self.client.post("/api/indexers/af-indexer-all-b-admin/actions", headers=headers, json={
            "operationId": str(uuid4()), "action": "run", "confirmed": True,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.gateway.calls, [])

    def test_mcp_status_never_returns_secret_settings(self):
        site = {"name": "mcp", "id": "/x/sites/mcp", "properties": {"state": "Stopped", "defaultHostName": "mcp.example"}}
        listed = {"properties": {"MCP_MODE": "hosted", "MCP_CLIENT_SECRET": "@Microsoft.KeyVault(secret)", "OTHER": "private"}}
        self.gateway.arm = Mock(side_effect=lambda method, path, *args, **kwargs: listed if method == "POST" else site)
        response = self.client.get("/api/integrations/mcp", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["settings"]["data"], {"values": {"MCP_MODE": "hosted"}, "legacyClientSecret": True})
        self.assertNotIn("KeyVault", response.get_data(as_text=True))
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertIsNone(response.json["probes"]["health"])

    def test_mcp_power_requires_confirmation_and_is_audited(self):
        self.gateway.arm = Mock(return_value=None)
        refused = self.client.post("/api/integrations/mcp/power", headers=self.headers, json={"action": "stop"})
        self.assertEqual(refused.status_code, 400)
        self.gateway.arm.assert_not_called()
        response = self.client.post("/api/integrations/mcp/power", headers=self.headers, json={"action": "stop", "confirmed": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.gateway.arm.call_args.args[1].endswith(f"/sites/{SETTINGS.mcp_app}/stop"))
        self.assertEqual(self.store.read()["audit"][0]["action"], "mcp_stop")

    def test_foundry_status_refuses_unexpected_project_endpoint(self):
        project = {"id": "/p", "properties": {"endpoints": {"AI Foundry API": "https://attacker.example/api/projects/p"}}}
        self.gateway.arm = Mock(return_value=project)
        self.gateway.resource = Mock(return_value={"name": "aif", "state": "Succeeded"})
        self.gateway.request = Mock()
        response = self.client.get("/api/integrations/foundry", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["sections"]["project"]["status"], "error")
        self.assertEqual(response.json["sections"]["agents"]["status"], "error")
        self.gateway.request.assert_not_called()
    def test_unconfirmed_action_is_rejected(self):
        response = self.client.post("/api/indexers/name/actions", headers=self.headers, json={"action": "run"})
        self.assertEqual(response.status_code, 400)

    def test_missing_action_fields_return_validation_error_not_500(self):
        response = self.client.post("/api/indexers/name/actions", headers=self.headers, json={"confirmed": True})
        self.assertEqual(response.status_code, 400)

    def test_invalid_metrics_window_is_rejected(self):
        response = self.client.get("/api/metrics?hours=999", headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_configuration_never_exposes_function_settings(self):
        response = self.client.get("/api/configuration", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("not-for-browser", response.get_data(as_text=True))
        self.assertEqual(response.json["permissionMode"], "source_group_mapping_not_ntfs")

    def test_group_maintenance_accepts_english_confirmation_over_http(self):
        preview = self.client.post("/api/group-changes/preview", headers=self.headers,
                                   json={"share": "admin-share", "groupId": GROUP_B})
        self.assertEqual(preview.status_code, 200)
        response = self.client.post("/api/group-changes/apply", headers=self.headers,
                                    json={"draftId": preview.json["id"], "confirmation": "REBUILD"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "queued")

    def test_index_schema_backup_is_not_sent_to_browser(self):
        self.store.data["operations"].append({"id": "test", "schema": {"apiKey": "private"}})
        response = self.client.get("/api/state", headers=self.headers)
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertIn("schema", self.store.data["operations"][0])

    def test_security_headers_on_ui(self):
        with self.client.get("/") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        self.controller = Controller(self.gateway, self.store)

    def draft(self):
        return self.controller.preview_group(ADMIN, "admin-share", GROUP_B)

    def operation(self):
        draft = self.draft()
        return self.controller.apply_group(ADMIN, draft["id"], "RECONSTRUIRE")

    def test_unknown_share_is_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.preview_group(ADMIN, "another-share", GROUP_B)

    def test_unchanged_group_is_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.preview_group(ADMIN, "admin-share", GROUP_A)

    def test_unknown_group_requires_directory_verification(self):
        with self.assertRaises(ValueError):
            self.controller.preview_group(ADMIN, "admin-share", CLIENT)

    def test_preview_has_no_cloud_mutation(self):
        draft = self.draft()
        self.assertTrue(draft["impact"]["maintenanceRequired"])
        self.assertFalse(draft["impact"]["sourceFilesDeleted"])
        self.assertEqual(self.gateway.mapping["admin-share"], GROUP_A)
        self.assertFalse(any(method != "GET" for method, _ in self.gateway.calls))

    def test_explicit_maintenance_confirmation_required(self):
        draft = self.draft()
        with self.assertRaisesRegex(ValueError, "Type REBUILD"):
            self.controller.apply_group(ADMIN, draft["id"], "Yes")

    def test_english_maintenance_confirmation_is_accepted(self):
        draft = self.draft()
        operation = self.controller.apply_group(ADMIN, draft["id"], "REBUILD")
        self.assertEqual(operation["status"], "queued")

    def test_expired_preview_is_rejected(self):
        draft = self.draft()
        self.store.data["drafts"][0]["expiresAt"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.assertRaises(StateConflict):
            self.controller.apply_group(ADMIN, draft["id"], "RECONSTRUIRE")

    def test_changed_live_configuration_requires_new_preview(self):
        draft = self.draft()
        self.gateway.indexers["af-indexer-all-b-admin"]["schedule"] = {"interval": "PT15M"}
        with self.assertRaises(StateConflict):
            self.controller.apply_group(ADMIN, draft["id"], "RECONSTRUIRE")

    def test_preexisting_search_download_mismatch_is_rejected(self):
        self.gateway.mapping["admin-share"] = GROUP_B
        with self.assertRaises(StateConflict):
            self.draft()

    def test_inactive_indexer_cannot_be_controlled(self):
        with self.assertRaises(AzureApiError):
            self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-admin", "run", None)

    def test_indexer_action_is_idempotent(self):
        operation_id = str(uuid4())
        self.controller.indexer_action(ADMIN, operation_id, "af-indexer-all-b-admin", "run", None)
        self.controller.indexer_action(ADMIN, operation_id, "af-indexer-all-b-admin", "run", None)
        self.assertEqual(sum(method == "POST" and path.endswith("/run") for method, path in self.gateway.calls), 1)

    def test_indexer_cannot_be_changed_during_maintenance(self):
        self.operation()
        with self.assertRaises(StateConflict):
            self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "run", None)

    def test_schedules_are_verified_after_update(self):
        result = self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "schedule", "PT15M")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.gateway.indexers["af-indexer-all-b-admin"]["schedule"], {"interval": "PT15M"})

    def test_unsupported_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "schedule", "PT1M")

    def test_full_permission_change_rebuilds_both_slots_and_restores_schedules(self):
        operation = self.operation()
        phases_seen = []
        for _ in range(60):
            phases_seen.append(operation["phase"])
            operation = self.controller.step(operation)
            if operation["status"] in TERMINAL:
                break
        self.assertEqual(operation["status"], "awaiting_access_verification")
        self.assertEqual(operation["documentCount"], 4)
        self.assertEqual(self.gateway.mapping["admin-share"], GROUP_B)
        self.assertEqual(self.gateway.mapping["alluser-share"], GROUP_B)
        for name, item in self.gateway.indexers.items():
            self.assertEqual(bool(item.get("schedule")), "-b-" in name)
        for name, item in self.gateway.skillsets.items():
            if name.endswith("-admin"):
                self.assertEqual(group_from_skillset(item), GROUP_B)
        for slot in ("af-index-all", "af-index-all-b"):
            self.assertIn(("DELETE", f"/indexes/{slot}"), self.gateway.calls)
            self.assertNotIn("never-persist-this-secret", json.dumps(self.gateway.indexes[slot]))
        self.assertIn("block_download", phases_seen)
        self.assertNotIn("schema", operation)

    def test_downloads_are_blocked_before_index_deletion(self):
        operation = self.operation()
        for _ in range(20):
            if operation["phase"] == "delete":
                break
            operation = self.controller.step(operation)
        self.assertNotIn("admin-share", self.gateway.mapping)
        self.assertFalse(any(method == "DELETE" for method, _ in self.gateway.calls))

    def test_failure_does_not_publish_new_download_permissions(self):
        operation = self.operation()
        self.gateway.fail_run = True
        with self.assertRaises(AzureApiError):
            for _ in range(60):
                operation = self.controller.step(operation)
        self.assertNotIn("admin-share", self.gateway.mapping)
        self.assertNotEqual(operation["status"], "completed")

    def test_resume_requires_failed_operation(self):
        operation = self.operation()
        with self.assertRaises(StateConflict):
            self.controller.retry(ADMIN, operation["id"])

    def test_unstarted_failed_maintenance_can_be_cancelled(self):
        operation = self.operation()
        self.store.data["operations"][0]["status"] = "failed"
        result = self.controller.cancel_unstarted(ADMIN, operation["id"])
        self.assertEqual(result["status"], "cancelled")
        self.controller._no_maintenance(self.store.read())

    def test_started_maintenance_cannot_be_cancelled(self):
        operation = self.operation()
        self.store.data["operations"][0].update(status="failed", phase="delete")
        with self.assertRaises(StateConflict):
            self.controller.cancel_unstarted(ADMIN, operation["id"])

    def test_cancelled_snapshot_is_not_overwritten_by_worker(self):
        operation = self.operation()
        self.store.claim = Mock(return_value=Mock())
        original_step = self.controller.step
        def cancel_during_snapshot(current):
            result = original_step(current)
            self.controller.cancel_unstarted(ADMIN, current["id"])
            return result
        self.controller.step = cancel_during_snapshot
        with self.assertRaises(StateConflict):
            Worker(self.controller).tick()
        self.assertEqual(self.store.data["operations"][0]["status"], "cancelled")

    def test_api_and_worker_share_one_azure_control_lock(self):
        self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "run", None)
        self.operation()
        Worker(self.controller).tick()
        self.assertTrue(all(call.args == ("azure-control",) for call in self.store.claim.call_args_list))
        self.assertEqual(len(self.store.claim.call_args_list), 3)

    def test_busy_azure_control_lock_prevents_any_mutation(self):
        error = HttpResponseError(message="Lease already owned.")
        error.status_code = 409
        self.store.claim.side_effect = error
        with patch("operations.time.sleep"), patch("operations.time.monotonic", side_effect=[0, 0, 31]):
            with self.assertRaises(StateConflict):
                self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "run", None)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.store.read()["operations"], [])

    def test_briefly_held_lock_is_awaited(self):
        error = HttpResponseError(message="Lease present.")
        error.status_code = 412
        self.store.claim.side_effect = [error, Mock()]
        with patch("operations.time.sleep"):
            result = self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "run", None)
        self.assertEqual(result["status"], "accepted")

    def test_repeated_apply_returns_existing_operation(self):
        operation = self.operation()
        self.gateway.mapping["admin-share"] = GROUP_B
        repeated = self.controller.apply_group(ADMIN, operation["id"], "RECONSTRUIRE")
        self.assertEqual(repeated["id"], operation["id"])

    def test_expired_maintenance_is_not_success(self):
        operation = self.operation()
        operation["deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.assertRaises(StateConflict):
            self.controller.step(operation)


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        self.controller = Controller(self.gateway, self.store)

    def run_rebuild(self, operation=None, limit=80):
        operation = operation or self.controller.start_rebuild(ADMIN, str(uuid4()), "rebuild")
        for _ in range(limit):
            operation = self.controller.step(operation)
            if operation["status"] in TERMINAL:
                break
        self.store.data["operations"] = [operation if op["id"] == operation["id"] else op
                                         for op in self.store.data["operations"]]
        return operation

    def test_rebuild_switches_agent_and_moves_schedules(self):
        self.gateway.indexers["af-indexer-all-b-alluser"]["schedule"] = {"interval": "PT1H"}
        operation = self.run_rebuild()
        self.assertEqual(operation["status"], "completed")
        self.assertTrue(operation["switched"])
        self.assertEqual(self.gateway.active, "af-index-all")
        self.assertEqual(operation["newChunks"], 4)
        self.assertIn(("DELETE", "/indexes/af-index-all"), self.gateway.calls)
        self.assertNotIn(("DELETE", "/indexes/af-index-all-b"), self.gateway.calls)
        self.assertIn(("POST", "/indexers/af-indexer-all-admin/reset"), self.gateway.calls)
        self.assertEqual(self.gateway.indexers["af-indexer-all-admin"]["schedule"], {"interval": "PT5M"})
        self.assertEqual(self.gateway.indexers["af-indexer-all-alluser"]["schedule"], {"interval": "PT1H"})
        self.assertNotIn("schedule", self.gateway.indexers["af-indexer-all-b-admin"])
        self.assertNotIn("schedule", self.gateway.indexers["af-indexer-all-b-alluser"])
        self.assertNotIn("never-persist-this-secret", json.dumps(self.gateway.indexes["af-index-all"]))
        self.assertEqual(self.gateway.sources()["activeIndex"], "af-index-all")

    def test_consecutive_rebuilds_alternate_slots(self):
        self.run_rebuild()
        operation = self.run_rebuild()
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(self.gateway.active, "af-index-all-b")
        self.assertEqual(self.gateway.indexers["af-indexer-all-b-admin"]["schedule"], {"interval": "PT5M"})
        self.assertNotIn("schedule", self.gateway.indexers["af-indexer-all-admin"])

    def test_missing_target_slot_is_created_from_active_slot(self):
        for name in ("af-indexer-all-admin", "af-indexer-all-alluser"):
            del self.gateway.indexers[name]
        for name in ("af-skills-all-admin", "af-skills-all-alluser"):
            del self.gateway.skillsets[name]
        self.gateway.statuses["af-indexer-all-admin"]["lastResult"]["startTime"] = None
        self.gateway.statuses["af-indexer-all-alluser"]["lastResult"]["startTime"] = None
        operation = self.run_rebuild()
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(self.gateway.indexers["af-indexer-all-admin"]["skillsetName"], "af-skills-all-admin")
        self.assertEqual(self.gateway.indexers["af-indexer-all-admin"]["targetIndexName"], "af-index-all")
        self.assertEqual(group_from_skillset(self.gateway.skillsets["af-skills-all-alluser"]), GROUP_B)
        selectors = self.gateway.skillsets["af-skills-all-admin"]["indexProjections"]["selectors"]
        self.assertEqual({item["targetIndexName"] for item in selectors}, {"af-index-all"})

    def test_purge_switches_when_deleted_files_reduce_chunk_count_by_more_than_half(self):
        self.gateway.counts = {"af-index-all-b": 94, "af-index-all": 37}
        operation = self.run_rebuild()
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(operation["newChunks"], 37)
        self.assertEqual(operation["previousChunks"], 94)
        self.assertEqual(self.gateway.active, "af-index-all")

    def test_purge_keeps_active_index_when_rebuilt_index_is_empty(self):
        self.gateway.counts = {"af-index-all-b": 100, "af-index-all": 0}
        with patch.object(index_rebuild, "STATS_TIMEOUT_SECONDS", 0):
            with self.assertRaises(StateConflict):
                self.run_rebuild()
        self.assertEqual(self.gateway.active, "af-index-all-b")

    def test_failed_indexing_keeps_active_index(self):
        self.gateway.statuses["af-indexer-all-admin"]["lastResult"]["status"] = "transientFailure"
        with self.assertRaises(StateConflict):
            self.run_rebuild()
        self.assertEqual(self.gateway.active, "af-index-all-b")

    def test_worker_marks_failure_and_resume_restarts_before_switch(self):
        self.controller.start_rebuild(ADMIN, str(uuid4()), "rebuild")
        self.gateway.fail_run = True
        worker = Worker(self.controller)
        for _ in range(20):
            worker.tick()
        operation = self.store.read()["operations"][0]
        self.assertEqual(operation["status"], "failed")
        self.assertFalse(operation["switched"])
        self.controller.preview_group(ADMIN, "admin-share", GROUP_B)
        resumed = self.controller.retry(ADMIN, operation["id"])
        self.assertEqual((resumed["status"], resumed["phase"]), ("queued", "prepare"))
        self.gateway.fail_run = False
        for _ in range(40):
            worker.tick()
        self.assertEqual(self.store.read()["operations"][0]["status"], "completed")
        self.assertEqual(self.gateway.active, "af-index-all")

    def test_failure_after_switch_blocks_other_operations_until_resumed(self):
        operation = self.controller.start_rebuild(ADMIN, str(uuid4()), "rebuild")
        self.store.data["operations"][0].update(status="failed", switched=True, phase="schedule",
                                                activeIndex="af-index-all-b", targetIndex="af-index-all")
        with self.assertRaises(StateConflict):
            self.controller.start_rebuild(ADMIN, str(uuid4()))
        with self.assertRaises(StateConflict):
            self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "run", None)
        resumed = self.controller.retry(ADMIN, operation["id"])
        self.assertEqual(resumed["phase"], "schedule")

    def test_rebuild_and_permission_maintenance_exclude_each_other(self):
        draft = self.controller.preview_group(ADMIN, "admin-share", GROUP_B)
        self.controller.start_rebuild(ADMIN, str(uuid4()))
        with self.assertRaises(StateConflict):
            self.controller.preview_group(ADMIN, "admin-share", GROUP_B)
        with self.assertRaises(StateConflict):
            self.controller.apply_group(ADMIN, draft["id"], "RECONSTRUIRE")
        with self.assertRaises(StateConflict):
            self.controller.indexer_action(ADMIN, str(uuid4()), "af-indexer-all-b-admin", "run", None)
        self.store.data["operations"][0]["status"] = "completed"
        self.controller.apply_group(ADMIN, draft["id"], "RECONSTRUIRE")
        with self.assertRaises(StateConflict):
            self.controller.start_rebuild(ADMIN, str(uuid4()))

    def chunk(self, key, share, path, account="shareiarag"):
        return {"chunk_id": key, "metadata_storage_path": f"https://{account}.file.core.windows.net/{share}/{quote(path)}"}

    def targeted(self, listing, **kwargs):
        def files(row):
            if isinstance(listing, Exception):
                raise listing
            return [{"sourceKey": row["sourceKey"], "account": row["account"], "share": row["share"],
                     "resourceGroup": row["resourceGroup"], "path": path} for path in listing.get(row["share"], [])]
        with patch("documents.source_files", side_effect=files), patch.object(index_rebuild, "PAGE_SIZE", 2):
            return self.run_rebuild(self.controller.start_rebuild(ADMIN, str(uuid4())), **kwargs)

    def test_targeted_purge_deletes_only_chunks_of_missing_files_without_reindexing(self):
        self.gateway.documents["af-index-all-b"] = [
            self.chunk("a1", "admin-share", "Dossier/Accès sortant.docx"),
            self.chunk("a2", "admin-share", "Dossier/Accès sortant.docx"),
            self.chunk("b'1", "admin-share", "gone.txt"),
            self.chunk("c1", "alluser-share", "other.txt"),
            self.chunk("d1", "old-share", "removed-source.txt", account="otherfiles"),
            {"chunk_id": "e1", "metadata_storage_path": "not a file url"},
        ]
        listing = {"admin-share": ["dossier/accès sortant.docx", "new-not-indexed-yet.txt"], "alluser-share": ["other.txt"]}
        operation = self.targeted(listing)
        self.assertEqual((operation["status"], operation["mode"]), ("completed", "targeted"))
        self.assertEqual(sorted(self.gateway.deleted), ["b'1", "d1"])
        self.assertEqual(self.gateway.active, "af-index-all-b")
        self.assertFalse(any(path.endswith(("/reset", "/run")) or method in ("PUT", "DELETE")
                             for method, path in self.gateway.calls))
        self.assertEqual([(item["sourceKey"], item["path"]) for item in operation["purgedFiles"]],
                         [("admin-share", "gone.txt"), ("otherfiles/old-share", "removed-source.txt")])
        self.assertEqual((operation["deletedChunks"], operation["skippedChunks"], operation["newChunks"]), (2, 1, 4))
        self.assertEqual(operation["itemsProcessed"], {"admin": 1, "alluser": 1})
        self.assertEqual(operation["sourcePaths"]["admin-share"]["paths"], ["Dossier/Accès sortant.docx"])
        row = next(row for row in index_rebuild.job_counters(self.gateway.sources(), self.store.data["operations"])["indexers"]
                   if row["active"] and row["sourceKey"] == "admin-share")
        self.assertEqual(row["lastPurge"], {"at": operation["purgedAt"], "indexedCount": 1, "purgedCount": 1})
        self.assertEqual(self.targeted(listing)["deletedChunks"], 0)

    def test_targeted_purge_deletes_nothing_when_a_share_cannot_be_listed(self):
        self.gateway.documents["af-index-all-b"] = [self.chunk("a1", "admin-share", "a.txt")]
        with self.assertRaises(AzureApiError):
            self.targeted(AzureApiError("Storage unavailable."))
        self.assertEqual(self.gateway.deleted, [])

    def test_denied_targeted_purge_falls_back_to_a_full_rebuild(self):
        self.gateway.documents["af-index-all-b"] = [self.chunk("a1", "admin-share", "gone.txt")]
        self.gateway.deny_delete = True
        operation = self.targeted({"admin-share": [], "alluser-share": []})
        self.assertEqual((operation["status"], operation["mode"]), ("completed", "rebuild"))
        self.assertIn("HTTP 403", operation["fallbackReason"])
        self.assertTrue(operation["switched"])
        self.assertEqual(self.gateway.active, "af-index-all")

    def test_rejected_elevated_read_falls_back_to_a_full_rebuild_without_deleting(self):
        self.gateway.documents["af-index-all-b"] = [self.chunk("a1", "admin-share", "gone.txt")]
        self.gateway.deny_inventory = 400
        operation = self.targeted({"admin-share": [], "alluser-share": []})
        self.assertEqual((operation["status"], operation["mode"]), ("completed", "rebuild"))
        self.assertIn("HTTP 400", operation["fallbackReason"])
        self.assertEqual(self.gateway.deleted, [])

    def test_failed_targeted_purge_resumes_as_targeted(self):
        self.controller.start_rebuild(ADMIN, str(uuid4()))
        self.store.data["operations"][0].update(status="failed")
        self.assertEqual(self.controller.retry(ADMIN, self.store.data["operations"][0]["id"])["phase"], "purge")

    def test_manual_start_is_idempotent(self):
        identifier_value = str(uuid4())
        first = self.controller.start_rebuild(ADMIN, identifier_value)
        second = self.controller.start_rebuild(ADMIN, identifier_value)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.read()["operations"]), 1)

    def test_enabling_schedule_never_runs_immediately_then_runs_once_per_slot(self):
        self.controller.update_rebuild_schedule(ADMIN, True, 1, 0)
        self.assertIsNone(self.controller.schedule_due_rebuild(datetime.now(timezone.utc)))
        later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
        operation = self.controller.schedule_due_rebuild(later)
        self.assertEqual((operation["trigger"], operation["actor"]), ("schedule", "scheduler"))
        self.store.data["operations"][0]["status"] = "completed"
        self.assertIsNone(self.controller.schedule_due_rebuild(later))
        self.assertEqual(len([op for op in self.store.read()["operations"] if op["type"] == "rebuild"]), 1)

    def test_disabled_schedule_never_runs(self):
        later = datetime.now(timezone.utc) + timedelta(days=3)
        self.assertIsNone(self.controller.schedule_due_rebuild(later))
        self.controller.update_rebuild_schedule(ADMIN, True, 24, 2)
        self.controller.update_rebuild_schedule(ADMIN, False, 24, 2)
        self.assertIsNone(self.controller.schedule_due_rebuild(later))

    def test_idle_worker_enqueues_due_rebuild(self):
        self.controller.update_rebuild_schedule(ADMIN, True, 24, 2)
        self.store.data["rebuild"]["lastSlot"] = "2000-01-01T00:00:00+00:00"
        Worker(self.controller).tick()
        self.assertEqual(self.store.read()["operations"][0]["type"], "rebuild")

    def test_schedule_validation(self):
        for values in ((True, 5, 2), (True, 24, 24), ("yes", 24, 2), (True, True, 2), (True, 24, 2.0)):
            with self.assertRaises(ValueError):
                self.controller.update_rebuild_schedule(ADMIN, *values)

    def test_slots_follow_paris_wall_clock_across_dst(self):
        settings = {**index_rebuild.DEFAULT_SCHEDULE, "intervalHours": 24, "startHour": 2}
        winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(index_rebuild.due_slot(settings, winter), datetime(2026, 1, 15, 1, tzinfo=timezone.utc))
        self.assertEqual(index_rebuild.due_slot(settings, summer), datetime(2026, 7, 15, 0, tzinfo=timezone.utc))
        six = {**settings, "intervalHours": 6}
        self.assertEqual(index_rebuild.next_slot(six, winter), datetime(2026, 1, 15, 13, tzinfo=timezone.utc))
        spring = datetime(2026, 3, 28, 12, tzinfo=timezone.utc)
        self.assertEqual(index_rebuild.next_slot(settings, spring), datetime(2026, 3, 29, 1, tzinfo=timezone.utc))


class RebuildWebTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        self.verifier = Mock()
        self.verifier.verify.return_value = Principal(ADMIN, "Test Admin")
        self.client = create_app(SETTINGS, self.gateway, self.store, self.verifier, start_worker=False).test_client()
        self.headers = {"Authorization": "Bearer test", "Origin": "http://localhost"}

    def test_state_exposes_schedule(self):
        response = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.json["rebuild"]["enabled"], False)
        self.assertIsNone(response.json["rebuild"]["nextRun"])
        self.assertNotIn("lastSlot", response.json["rebuild"])

    def test_schedule_requires_confirmation_and_valid_values(self):
        response = self.client.post("/api/rebuild/schedule", headers=self.headers,
                                    json={"enabled": True, "intervalHours": 24, "startHour": 2})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/rebuild/schedule", headers=self.headers,
                                    json={"enabled": True, "intervalHours": 7, "startHour": 2, "confirmed": True})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/rebuild/schedule", headers=self.headers,
                                    json={"enabled": True, "intervalHours": 6, "startHour": 3, "confirmed": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["enabled"])
        self.assertEqual(self.store.read()["rebuild"]["updatedBy"], ADMIN)
        self.assertEqual(self.store.read()["operations"], [])

    def test_clear_logs_empties_the_log_but_keeps_records_the_worker_needs(self):
        self.store.data["operations"] = [
            {"id": "done", "type": "indexer", "status": "completed", "createdAt": "1"},
            {"id": "removed", "type": "source_remove", "status": "accepted", "createdAt": "1"},
            {"id": "failed-run", "type": "indexer", "status": "failed", "createdAt": "1"},
            {"id": "running", "type": "source_add", "status": "running", "createdAt": "1"},
            {"id": "resume", "type": "group_change", "status": "failed", "createdAt": "1"},
            {"id": "verify", "type": "source_add", "status": "awaiting_access_verification", "createdAt": "1"},
            {"id": "purge", "type": "rebuild", "status": "completed", "createdAt": "1"},
        ]
        self.store.data["audit"] = [{"action": "run"}]
        self.assertEqual(self.client.post("/api/logs/clear", headers=self.headers, json={}).status_code, 400)
        response = self.client.post("/api/logs/clear", headers=self.headers, json={"confirmed": True})
        self.assertEqual(response.json, {"removed": 7})
        state = self.store.read()
        self.assertEqual([op["id"] for op in state["operations"]], ["running", "resume", "purge"])
        self.assertIn("logsClearedAt", state)
        self.assertEqual(state["audit"], [])

    def test_manual_rebuild_requires_confirmation(self):
        response = self.client.post("/api/rebuild/run", headers=self.headers, json={"operationId": str(uuid4())})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/rebuild/run", headers=self.headers,
                                    json={"operationId": str(uuid4()), "confirmed": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["type"], "rebuild")
        self.assertEqual(self.gateway.calls, [])

    def test_cross_origin_rebuild_is_denied(self):
        headers = {**self.headers, "Origin": "https://attacker.example"}
        response = self.client.post("/api/rebuild/run", headers=headers,
                                    json={"operationId": str(uuid4()), "confirmed": True})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.read()["operations"], [])


class ConnectionGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.download_config = {"MULTI_ACCOUNT_SOURCES_VERSION": "1"}

    def download_settings(self):
        return {**self.download_config, "SHARE_GROUPS": ";".join(f"{key}={value}" for key, value in self.mapping.items())}

    def arm(self, method, path, body=None, version="2024-11-01", expected=(200,)):
        if path.endswith("/config/appsettings") and method == "PUT":
            self.download_config = copy.deepcopy(body["properties"])
            self.mapping = parse_share_groups(self.download_config.pop("SHARE_GROUPS"))
            return {}
        if "/fileServices/default/shares/" in path and method == "GET":
            return {"properties": {"enabledProtocols": "SMB"}}
        raise AssertionError(f"Unexpected ARM {method} {path}")


class SourceConnectionTests(unittest.TestCase):
    def setUp(self):
        self.gateway = ConnectionGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        self.controller = Controller(self.gateway, self.store)
        self.connection = "DefaultEndpointsProtocol=https;AccountName=otherfiles;AccountKey=private-test-key;EndpointSuffix=core.windows.net"
        self.environment = patch.dict("os.environ", {"FILES_CONNECTION_OTHERFILES": self.connection})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        listing = patch("documents.source_files", return_value=[])
        listing.start()
        self.addCleanup(listing.stop)

    def add(self, share="admin-share", operation_id=None, interval="PT15M"):
        return self.controller.add_source(ADMIN, operation_id or str(uuid4()), "other-rg", "otherfiles", share, GROUP_B, interval)

    def finish(self):
        worker = Worker(self.controller)
        for _ in range(90):
            worker.tick()
            operation = self.store.read()["operations"][0]
            if operation["status"] in TERMINAL and not any(
                    op["type"] in ("rebuild", "source_add", "group_change") and op["status"] not in TERMINAL
                    for op in self.store.read()["operations"]):
                return operation
        self.fail("Background operation did not terminate.")

    def test_connection_is_durable_and_creates_both_slots_without_exposing_credentials(self):
        queued = self.add()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(len(self.gateway.data_sources), 2)
        result = self.finish()
        self.assertEqual(result["status"], "awaiting_access_verification", result.get("error"))
        rows = [row for row in self.gateway.sources()["indexers"] if row["sourceKey"] == "otherfiles/admin-share"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["groupId"] for row in rows}, {GROUP_B})
        self.assertEqual(next(row["schedule"] for row in rows if row["active"]), "PT15M")
        self.assertIsNone(next(row["schedule"] for row in rows if not row["active"]))
        self.assertTrue(self.gateway.indexers[next(row["name"] for row in rows if not row["active"])]["disabled"])
        self.assertFalse(self.gateway.indexers[next(row["name"] for row in rows if row["active"])]["disabled"])
        self.assertEqual(self.gateway.mapping["otherfiles/admin-share"], GROUP_B)
        self.assertEqual(self.gateway.download_config["FILES_ACCOUNT_KEY_OTHERFILES"],
                         "@Microsoft.KeyVault(VaultName=kv-test-vault;SecretName=files-connection-otherfiles)")
        self.assertNotIn("private-test-key", json.dumps(self.gateway.download_config))
        data = json.dumps({"state": self.store.read(), "sources": self.gateway.sources()})
        self.assertNotIn("private-test-key", data)
        self.assertNotIn("private-connection", data)
        self.assertNotIn("connectionString", data)

    def test_same_share_name_in_other_account_has_independent_permissions(self):
        self.add()
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        draft = self.controller.preview_group(ADMIN, "otherfiles/admin-share", GROUP_A)
        self.controller.apply_group(ADMIN, draft["id"], "RECONSTRUIRE")
        result = self.finish()
        self.assertEqual(result["status"], "awaiting_access_verification", result.get("error"))
        self.assertEqual(self.gateway.mapping["admin-share"], GROUP_A)
        self.assertEqual(self.gateway.mapping["otherfiles/admin-share"], GROUP_A)
        original = [row for row in self.gateway.sources()["indexers"] if row["sourceKey"] == "admin-share"]
        self.assertEqual({row["groupId"] for row in original}, {GROUP_A})
        untouched = [row for row in self.gateway.sources()["indexers"] if row["sourceKey"] == "alluser-share"]
        self.assertEqual({row["groupId"] for row in untouched}, {GROUP_B})

    def test_rebuild_indexes_all_sources_and_preserves_individual_schedules(self):
        self.add()
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        self.controller.start_rebuild(ADMIN, str(uuid4()), "rebuild")
        result = self.finish()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(len(result["sourceNames"]), 3)
        active = [row for row in self.gateway.sources()["indexers"] if row["active"]]
        self.assertEqual(len(active), 3)
        self.assertEqual(next(row["schedule"] for row in active if row["account"] == "otherfiles"), "PT15M")
        self.assertTrue(all(not row["schedule"] for row in self.gateway.sources()["indexers"] if not row["active"]))

    def test_removed_source_leaves_no_azure_objects_and_is_purged_from_the_index(self):
        self.add()
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        self.gateway.documents["af-index-all-b"] = [
            {"chunk_id": "kept", "metadata_storage_path": "https://shareiarag.file.core.windows.net/admin-share/a.txt"},
            {"chunk_id": "removed", "metadata_storage_path": "https://otherfiles.file.core.windows.net/admin-share/a.txt"}]
        with patch("documents.source_files", side_effect=lambda row: [{**{key: row[key] for key in (
                "sourceKey", "account", "share", "resourceGroup")}, "path": "a.txt"}]):
            result = self.controller.remove_source(ADMIN, str(uuid4()), "otherfiles/admin-share")
            rebuild = self.store.read()["operations"][0]
            self.assertEqual((rebuild["type"], rebuild["id"], rebuild["mode"]), ("rebuild", result["rebuildId"], "targeted"))
            self.assertEqual(self.finish()["status"], "completed")
        self.assertEqual(self.gateway.deleted, ["removed"])
        self.assertEqual(self.gateway.active, "af-index-all-b", "removing a share no longer rebuilds the index")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(row["sourceKey"] == "otherfiles/admin-share" for row in self.gateway.sources()["indexers"]))
        self.assertEqual(len(self.gateway.indexers), 4)
        self.assertEqual(len(self.gateway.skillsets), 4)
        self.assertEqual(set(self.gateway.data_sources), {"af-ds-admin", "af-ds-alluser"})
        self.assertNotIn("otherfiles/admin-share", self.gateway.mapping)
        self.assertNotIn("FILES_ACCOUNT_KEY_OTHERFILES", self.gateway.download_config)

    def test_failed_connection_can_be_removed_and_so_can_the_last_source(self):
        operation = self.add()
        self.gateway.fail_run = True
        self.assertEqual(self.finish()["status"], "failed")
        self.gateway.fail_run = False
        self.controller.remove_source(ADMIN, str(uuid4()), "otherfiles/admin-share")
        cancelled = next(op for op in self.store.read()["operations"] if op["id"] == operation["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        first = self.store.read()["operations"][0]
        self.controller.remove_source(ADMIN, str(uuid4()), "admin-share")
        rebuilds = [op for op in self.store.read()["operations"] if op["type"] == "rebuild"]
        self.assertEqual([op["id"] for op in rebuilds], [first["id"]])
        self.assertEqual(self.finish()["status"], "completed")
        self.controller.remove_source(ADMIN, str(uuid4()), "alluser-share")
        self.assertEqual(self.gateway.sources()["indexers"], [])
        self.assertEqual(self.finish()["status"], "completed")
        self.assertIn("sourceTemplate", self.store.read())
        self.assertEqual(self.gateway.mapping, {})
        self.gateway.verify_group = Mock()
        self.add(share="newshare")
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        self.assertEqual(len([row for row in self.gateway.sources()["indexers"] if row["active"]]), 1)

    def test_failed_connection_is_visible_blocks_rebuild_and_resumes(self):
        operation = self.add()
        self.gateway.fail_run = True
        failed = self.finish()
        self.assertEqual(failed["status"], "failed")
        with self.assertRaises(StateConflict):
            self.controller.start_rebuild(ADMIN, str(uuid4()))
        self.gateway.fail_run = False
        self.controller.retry(ADMIN, operation["id"])
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        self.assertEqual(len(self.gateway.data_sources), 3)

    def test_replayed_step_does_not_duplicate_source_or_overwrite_conflicting_skillset(self):
        operation = self.add()
        operation = self.controller.step(operation)
        created = self.controller.step(operation)
        self.assertEqual(self.controller.step(operation)["phase"], created["phase"])
        step = self.controller.step(created)
        self.assertEqual(self.controller.step(created)["cursor"], step["cursor"])
        target = source_connections.names("otherfiles", "admin-share", "af-index-all")
        self.gateway.skillsets[target["skillset"]]["description"] = "externally changed"
        with self.assertRaises(StateConflict):
            self.controller.step(created)

    def test_missing_credentials_or_old_downloader_do_not_queue_a_connection(self):
        with patch.dict("os.environ", {"FILES_CONNECTION_OTHERFILES": ""}):
            with self.assertRaises(ValueError):
                self.add()
        self.gateway.download_config.clear()
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(self.store.read()["operations"], [])

    def test_new_indexers_are_disabled_until_download_configuration_is_published(self):
        self.add()
        worker = Worker(self.controller)
        for _ in range(12):
            if self.store.read()["operations"][0]["phase"] == "download":
                break
            worker.tick()
        self.assertEqual(self.store.read()["operations"][0]["phase"], "download")
        for index in ("af-index-all", "af-index-all-b"):
            target = source_connections.names("otherfiles", "admin-share", index)
            self.assertTrue(self.gateway.indexers[target["name"]]["disabled"])
            self.assertNotIn(("POST", f"/indexers/{target['name']}/run"), self.gateway.calls)
        self.assertNotIn("otherfiles/admin-share", self.gateway.mapping)
        worker.tick()
        self.assertEqual(self.gateway.mapping["otherfiles/admin-share"], GROUP_B)
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")

    def test_paused_source_indexes_once_without_enabling_a_schedule(self):
        self.add(interval=None)
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        rows = [row for row in self.gateway.sources()["indexers"] if row["sourceKey"] == "otherfiles/admin-share"]
        self.assertTrue(all(row["schedule"] is None for row in rows))

    def test_failed_items_require_a_new_successful_execution_after_resume(self):
        operation = self.add()
        worker = Worker(self.controller)
        for _ in range(15):
            if self.store.read()["operations"][0]["phase"] == "wait":
                break
            worker.tick()
        name = source_connections.names("otherfiles", "admin-share", self.gateway.active)["name"]
        old_start = self.gateway.statuses[name]["lastResult"]["startTime"]
        self.gateway.statuses[name]["lastResult"]["itemsFailed"] = 1
        worker.tick()
        self.assertEqual(self.store.read()["operations"][0]["status"], "failed")
        self.controller.retry(ADMIN, operation["id"])
        self.gateway.statuses[name]["lastResult"]["itemsFailed"] = 0
        self.assertEqual(self.finish()["status"], "awaiting_access_verification")
        self.assertNotEqual(self.gateway.statuses[name]["lastResult"]["startTime"], old_start)

    def test_connection_string_cannot_override_endpoints_or_use_ambiguous_keys(self):
        for suffix in (";FileEndpoint=https://example.test", ";AccountName=otherfiles", ";unexpected=value", ";malformed"):
            with self.subTest(suffix=suffix), patch.dict("os.environ", {"FILES_CONNECTION_OTHERFILES": self.connection + suffix}):
                with self.assertRaises(ValueError):
                    self.add()
        self.assertEqual(self.store.read()["operations"], [])

    def test_add_is_idempotent_but_rejects_reuse_for_a_different_request(self):
        operation_id = str(uuid4())
        first = self.add(operation_id=operation_id)
        self.assertEqual(self.add(operation_id=operation_id), first)
        with self.assertRaises(StateConflict):
            self.add(share="another-share", operation_id=operation_id)
        self.assertEqual(len(self.store.read()["operations"]), 1)

    def test_duplicate_account_share_and_unsupported_protocol_are_rejected(self):
        self.add()
        self.finish()
        with self.assertRaises(StateConflict):
            self.add()
        self.gateway.arm = Mock(return_value={"properties": {"enabledProtocols": "NFS"}})
        with self.assertRaises(ValueError):
            self.add(share="another-share")

    def test_invalid_identifiers_cannot_select_another_subscription(self):
        for rg, account, share in (
            ("../other-subscription", "otherfiles", "documents"),
            ("other-rg", "https://evil", "documents"),
            ("other-rg", "otherfiles", "../documents"),
        ):
            with self.subTest(rg=rg, account=account, share=share), self.assertRaises(ValueError):
                self.controller.add_source(ADMIN, str(uuid4()), rg, account, share, GROUP_B, "PT5M")


class DirectoryGroupTests(unittest.TestCase):
    def setUp(self):
        self.gateway = AzureGateway(replace(SETTINGS, directory_enabled=True), Mock())
        self.gateway.request = Mock()
        self.first = {"id": GROUP_A, "displayName": "Team Alpha", "securityEnabled": True}
        self.second = {"id": GROUP_B, "displayName": "Team Beta", "securityEnabled": True}

    def test_search_filters_security_groups_and_escapes_the_name(self):
        self.gateway.request.return_value = {"value": [
            self.first, {"id": GROUP_B, "displayName": "Mail", "securityEnabled": False},
        ]}
        result = self.gateway.groups("Team's")
        query = parse_qs(urlsplit(self.gateway.request.call_args.args[1]).query)
        self.assertEqual(query["$filter"], ["securityEnabled eq true and startswith(displayName,'Team''s')"])
        self.assertEqual(query["$select"], ["id,displayName,securityEnabled"])
        self.assertEqual(query["$top"], ["50"])
        self.assertEqual(result["groups"], [{"id": GROUP_A, "displayName": "Team Alpha"}])
        self.assertIsNone(result["nextCursor"])

    def test_pagination_reconstructs_requests_from_an_opaque_token(self):
        token = "page +/="
        link = "https://graph.microsoft.com/v1.0/groups?" + urlencode({"$skiptoken": token})
        self.gateway.request.side_effect = [
            {"value": [self.first], "@odata.nextLink": link}, {"value": [self.second]},
        ]
        first = self.gateway.groups("Team")
        self.assertEqual(first["nextCursor"], token)
        self.assertTrue(first["moreAvailable"])
        second = self.gateway.groups("Team", first["nextCursor"])
        query = parse_qs(urlsplit(self.gateway.request.call_args.args[1]).query)
        self.assertEqual(query["$skiptoken"], [token])
        self.assertEqual(query["$filter"], ["securityEnabled eq true and startswith(displayName,'Team')"])
        self.assertEqual(second["groups"][0]["id"], GROUP_B)
        self.assertFalse(second["moreAvailable"])

    def test_untrusted_or_invalid_continuation_links_fail_explicitly(self):
        for link in (
            "https://example.test/v1.0/groups?$skiptoken=x",
            "https://graph.microsoft.com/v1.0/users?$skiptoken=x",
            "https://graph.microsoft.com/v1.0/groups?other=x",
            "https://graph.microsoft.com/v1.0/groups?$skiptoken=x#fragment",
            "https://[invalid/v1.0/groups?$skiptoken=x",
            "", None, 42,
        ):
            with self.subTest(link=link):
                self.gateway.request.return_value = {"value": [self.first], "@odata.nextLink": link}
                with self.assertRaises(AzureApiError):
                    self.gateway.groups()

    def test_guid_search_checks_one_group_and_handles_missing_or_nonsecurity_groups(self):
        for group, expected in ((self.first, [GROUP_A]), (None, []),
                                ({"id": GROUP_A, "displayName": "Mail", "securityEnabled": False}, [])):
            with self.subTest(group=group):
                self.gateway.request.return_value = group
                result = self.gateway.groups(GROUP_A)
                self.assertEqual([item["id"] for item in result["groups"]], expected)
                self.assertIn(f"/groups/{GROUP_A}?", self.gateway.request.call_args.args[1])
                self.assertEqual(self.gateway.request.call_args.kwargs["expected"], (200, 404))
        with self.assertRaises(ValueError):
            self.gateway.groups(GROUP_A, "cursor")

    def test_bad_metadata_invalid_inputs_and_graph_errors_are_not_hidden(self):
        for invalid in ({"value": [{}]}, {"value": [{"id": "bad", "displayName": "Team", "securityEnabled": True}]}, {}):
            with self.subTest(invalid=invalid):
                self.gateway.request.return_value = invalid
                with self.assertRaises(AzureApiError):
                    self.gateway.groups()
        for search, cursor in (("x" * 257, ""), ("bad\nname", ""), ("", "x" * 8193), ("", "bad\ncursor")):
            with self.subTest(search=search, cursor=cursor), self.assertRaises(ValueError):
                self.gateway.groups(search, cursor)
        self.gateway.request.side_effect = AzureApiError("Graph unavailable.")
        with self.assertRaisesRegex(AzureApiError, "Graph unavailable"):
            self.gateway.groups()

    def test_directory_disabled_only_exposes_existing_configured_groups(self):
        self.gateway.settings = replace(SETTINGS, directory_enabled=False)
        self.gateway.sources = Mock(return_value={"indexers": [{"groupId": GROUP_A}, {"groupId": GROUP_B}]})
        result = self.gateway.groups(GROUP_A[:8])
        self.assertEqual(result["directoryStatus"], "not_configured")
        self.assertEqual(result["groups"], [{"id": GROUP_A, "displayName": GROUP_A}])
        self.gateway.request.assert_not_called()
        with self.assertRaises(ValueError):
            self.gateway.groups("", "cursor")


class ResourceDiscoveryTests(unittest.TestCase):
    def test_accounts_include_resource_group_without_secret_properties(self):
        gateway = AzureGateway(SETTINGS, Mock())
        gateway.arm = Mock(return_value={"value": [{
            "id": f"/subscriptions/{SUB}/resourceGroups/other-rg/providers/Microsoft.Storage/storageAccounts/otherfiles",
            "name": "otherfiles", "properties": {"private": "do-not-export"},
        }]})
        result = gateway.storage_accounts()
        self.assertEqual(result["subscriptionId"], SUB)
        self.assertEqual(result["accounts"][0]["resourceGroup"], "other-rg")
        self.assertNotIn("do-not-export", json.dumps(result))

    def test_pagination_is_followed_only_inside_configured_subscription(self):
        gateway = AzureGateway(SETTINGS, Mock())
        link = f"https://management.azure.com/subscriptions/{SUB}/providers/Microsoft.Storage/storageAccounts?next=1"
        gateway.arm = Mock(return_value={"value": [1], "nextLink": link})
        gateway.request = Mock(return_value={"value": [2]})
        self.assertEqual(gateway.arm_collection("/path"), [1, 2])
        gateway.arm.return_value["nextLink"] = "https://example.test/other"
        with self.assertRaises(AzureApiError):
            gateway.arm_collection("/path")

    def test_unknown_or_wrong_subscription_source_metadata_is_explicit_error(self):
        for data in (
            {"name": "arbitrary", "container": {"name": "documents"}},
            {"name": "arbitrary", "container": {"name": "documents"}, "description": source_connections.PREFIX + json.dumps(
                {"subscriptionId": TENANT, "resourceGroup": "other-rg", "account": "otherfiles"})},
        ):
            with self.subTest(data=data), self.assertRaises(AzureApiError):
                source_connections.metadata(SETTINGS, data)


class SourceConnectionWebTests(unittest.TestCase):
    def setUp(self):
        self.gateway = ConnectionGateway()
        self.store = MemoryState()
        self.store.claim = Mock(return_value=Mock())
        verifier = Mock()
        verifier.verify.return_value = Principal(ADMIN, "Test Admin")
        self.client = create_app(SETTINGS, self.gateway, self.store, verifier, start_worker=False).test_client()
        self.headers = {"Authorization": "Bearer test", "Origin": "http://localhost"}
        self.body = {"operationId": str(uuid4()), "resourceGroup": "other-rg", "account": "otherfiles",
                     "share": "documents", "groupId": GROUP_B, "interval": "PT5M", "confirmed": True}

    def test_add_requires_auth_confirmation_and_same_origin(self):
        self.assertEqual(self.client.post("/api/sources", json=self.body).status_code, 401)
        self.assertEqual(self.client.post("/api/sources", json={**self.body, "confirmed": False}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/api/sources", json=self.body, headers={**self.headers, "Origin": "https://other.test"}).status_code, 403)
        self.assertEqual(self.store.read()["operations"], [])

    def test_missing_storage_connection_returns_a_safe_error_without_queueing(self):
        for value in ("", "invalid-secret-value"):
            with self.subTest(value=value), patch.dict("os.environ", {"FILES_CONNECTION_OTHERFILES": value}):
                response = self.client.post("/api/sources", json=self.body, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["code"], "missing_storage_connection")
            self.assertEqual(response.json["setting"], "FILES_CONNECTION_OTHERFILES")
            self.assertNotIn("invalid-secret-value", response.get_data(as_text=True))
            self.assertEqual(self.store.read()["operations"], [])

    def test_unresolved_key_vault_reference_is_reported_without_queueing(self):
        reference = "@Microsoft.KeyVault(VaultName=kv-test-vault;SecretName=files-connection-otherfiles)"
        with patch.dict("os.environ", {"FILES_CONNECTION_OTHERFILES": reference}):
            response = self.client.post("/api/sources", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "storage_connection_unresolved")
        self.assertIn("files-connection-otherfiles", response.json["error"])
        self.assertEqual(self.store.read()["operations"], [])

    def test_unready_downloader_returns_a_specific_error_without_queueing(self):
        self.gateway.download_config.clear()
        with patch.dict("os.environ", {
            "FILES_CONNECTION_OTHERFILES": "DefaultEndpointsProtocol=https;AccountName=otherfiles;AccountKey=private-test-key",
        }):
            response = self.client.post("/api/sources", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "downloader_not_ready")
        self.assertNotIn("private-test-key", response.get_data(as_text=True))
        self.assertEqual(self.store.read()["operations"], [])

    def test_queue_and_state_responses_never_contain_connection_secrets(self):
        with patch.dict("os.environ", {"FILES_CONNECTION_OTHERFILES": "DefaultEndpointsProtocol=https;AccountName=otherfiles;AccountKey=private-test-key"}):
            response = self.client.post("/api/sources", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["sourceKey"], "otherfiles/documents")
        for path in ("/api/state", "/api/sources", "/api/configuration"):
            result = self.client.get(path, headers=self.headers)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn("private-test-key", result.get_data(as_text=True))
            self.assertNotIn("private-connection", result.get_data(as_text=True))

    def test_discovery_is_protected_and_rejects_invalid_paths(self):
        self.assertEqual(self.client.get("/api/resources/accounts").status_code, 401)
        response = self.client.get("/api/resources/shares?resourceGroup=../other&account=otherfiles", headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
