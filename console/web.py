import logging
from functools import wraps

from azure.identity import AzureCliCredential, ManagedIdentityCredential
from flask import Flask, g, jsonify, request
from werkzeug.exceptions import BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import AuthenticationError, AuthorizationError, TokenVerifier
from azure_gateway import AzureApiError, AzureGateway
from documents import paginate
from index_rebuild import MODES, job_counters, job_files
import integrations
from operations import Controller, Worker, audit
from settings import Settings, identifier
from source_connections import SourcePrerequisiteError
from state import BlobState, StateConflict, StateError


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)


class _CachedCredential:
    """Local development only: AzureCliCredential spawns `az` on every call and never caches tokens."""

    def __init__(self, inner):
        import threading
        self._inner, self._tokens, self._lock = inner, {}, threading.Lock()

    def get_token(self, *scopes, **kwargs):
        import time
        key = (scopes, tuple(sorted(kwargs.items())))
        with self._lock:
            token = self._tokens.get(key)
            if token is None or token.expires_on - time.time() < 300:
                token = self._tokens[key] = self._inner.get_token(*scopes, **kwargs)
            return token


def create_app(settings=None, gateway=None, store=None, verifier=None, start_worker=True):
    import os

    settings = settings or Settings.from_env()
    credential = None
    if gateway is None or store is None:
        credential = (
            ManagedIdentityCredential(connection_timeout=5, read_timeout=5, retry_total=1)
            if os.environ.get("WEBSITE_INSTANCE_ID") else _CachedCredential(AzureCliCredential(process_timeout=10))
        )
    gateway = gateway or AzureGateway(settings, credential)
    store = store or BlobState(settings, credential)
    verifier = verifier or TokenVerifier(settings)
    controller = Controller(gateway, store)
    worker = Worker(controller)
    application = Flask(__name__, static_folder="static", static_url_path="/static")
    if os.environ.get("WEBSITE_INSTANCE_ID"):
        application.wsgi_app = ProxyFix(application.wsgi_app, x_proto=1)
    application.config.update(MAX_CONTENT_LENGTH=16 * 1024)
    application.extensions.update(controller=controller, worker=worker)

    @application.before_request
    def authenticate():
        if request.path.startswith("/api/"):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or len(header) > 16000:
                raise AuthenticationError("Sign in with Microsoft Entra ID.")
            g.principal = verifier.verify(header[7:])
            if request.method != "GET":
                if request.headers.get("Origin") != request.host_url.rstrip("/"):
                    raise AuthorizationError("Cross-origin administration requests are not allowed.")
                if request.mimetype != "application/json":
                    raise ValueError("A JSON request body is required.")
            if start_worker:
                worker.start()

    @application.after_request
    def security_headers(response):
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' https://login.microsoftonline.com; img-src 'self' data:; frame-src https://login.microsoftonline.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def handle_errors(function):
        @wraps(function)
        def wrapped(error):
            logging.warning("Console request rejected: %s.", type(error).__name__)
            return jsonify({"error": str(error)}), function(error)
        return wrapped

    for kind, status in (
        (AuthenticationError, 401), (AuthorizationError, 403), (ValueError, 400),
        (BadRequest, 400), (StateConflict, 409), (StateError, 503),
    ):
        application.register_error_handler(kind, handle_errors(lambda _error, code=status: code))
    application.register_error_handler(AzureApiError, handle_errors(lambda error: error.status))

    @application.errorhandler(SourcePrerequisiteError)
    def source_prerequisite_error(error):
        logging.warning("Console request rejected: %s.", type(error).__name__)
        result = {"error": str(error), "code": error.code}
        if error.setting:
            result["setting"] = error.setting
        return jsonify(result), 400

    @application.get("/")
    @application.get("/auth/callback")
    def index():
        return application.send_static_file("index.html")

    @application.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @application.get("/public-config")
    def public_config():
        return jsonify({
            "clientId": settings.client_id, "tenantId": settings.tenant,
            "scope": f"api://{settings.client_id}/Console.Access",
            "searchScope": "https://search.azure.com/user_impersonation",
            "resourceGroup": settings.resource_group, "searchService": settings.search_name,
            "subscriptionId": settings.subscription,
        })

    @application.get("/api/me")
    def me():
        return jsonify({"objectId": g.principal.object_id, "name": g.principal.name, "role": "Administrator"})

    def job_records():
        try:
            data = store.read()
        except StateError:
            logging.warning("Job counters unavailable: console state could not be read.")
            return [], {}
        return data["operations"], data.get("syncFiles", {})

    def with_job_counters(sources):
        return job_counters(sources, *job_records())

    @application.get("/api/overview")
    def overview():
        data = gateway.overview()
        section = data["sections"].get("sources")
        if section and section["status"] == "ok":
            section["data"] = with_job_counters(section["data"])
        return jsonify(data)

    @application.get("/api/sources")
    def sources():
        return jsonify(with_job_counters(gateway.source_names(gateway.sources())))

    def pagination():
        try:
            offset = int(request.args.get("offset", "0"))
            limit = int(request.args.get("limit", "100"))
        except ValueError as error:
            raise ValueError("Choose valid pagination numbers.") from error
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Choose a nonnegative offset and a page size from 1 to 100.")
        return offset, limit

    @application.get("/api/sources/files")
    def source_job_files():
        offset, limit = pagination()
        kind, source_key = request.args.get("kind", ""), request.args.get("sourceKey", "")
        data = store.read()
        files, at = job_files(gateway.sources(), data["operations"], data.get("syncFiles", {}), source_key, kind,
                              request.args.get("slot") == "standby")
        if files is None:
            raise AzureApiError("No completed job recorded this file list yet.", 409)
        return jsonify({**paginate([{"sourceKey": source_key, "path": path} for path in files], offset, limit), "at": at})

    @application.get("/api/operations/<operation_id>/files")
    def operation_files(operation_id):
        operation_id = identifier(operation_id)
        offset, limit = pagination()
        operation = next((op for op in store.read()["operations"] if op["id"] == operation_id), None)
        if not operation or operation["type"] != "rebuild":
            raise ValueError("Choose a recorded purge operation.")
        if not (operation.get("switched") or operation.get("purgedAt")) or "purgedFiles" not in operation:
            raise AzureApiError(operation.get("inventoryError") or "No verified purged-file list was recorded for this operation.", 409)
        return jsonify(paginate(operation["purgedFiles"], offset, limit))

    @application.get("/api/resources/accounts")
    def storage_accounts():
        return jsonify(gateway.storage_accounts())

    @application.get("/api/resources/shares")
    def storage_shares():
        return jsonify(gateway.storage_shares(request.args.get("resourceGroup"), request.args.get("account")))

    @application.post("/api/sources")
    def add_source():
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True:
            raise ValueError("Confirm the source connection explicitly.")
        return jsonify(controller.add_source(
            g.principal.object_id, body.get("operationId"), body.get("resourceGroup"), body.get("account"),
            body.get("share"), body.get("groupId"), body.get("interval"),
        )), 202

    @application.get("/api/groups")
    def groups():
        return jsonify(gateway.groups(request.args.get("search", ""), request.args.get("cursor", "")))

    @application.get("/api/metrics")
    def metrics():
        hours = request.args.get("hours", "24")
        if hours not in ("1", "6", "24", "168"):
            raise ValueError("Choose a supported metrics time range.")
        return jsonify(gateway.metrics(int(hours)))

    @application.get("/api/state")
    def state():
        data = store.read()
        data.pop("documents", None)
        data.pop("syncFiles", None)
        data.pop("sourceTemplate", None)
        for operation in data["operations"]:
            operation.pop("schema", None)
            operation.pop("template", None)
            operation.pop("previousFiles", None)
            operation.pop("purgedFiles", None)
            operation.pop("sourcePaths", None)
        data["worker"] = {"running": worker.alive, "error": worker.last_error}
        data["rebuild"] = controller.rebuild_view(data)
        return jsonify(data)

    @application.post("/api/rebuild/schedule")
    def rebuild_schedule():
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True:
            raise ValueError("Confirm the rebuild schedule change explicitly.")
        return jsonify(controller.update_rebuild_schedule(
            g.principal.object_id, body.get("enabled"), body.get("intervalHours"), body.get("startHour"),
        ))

    @application.post("/api/logs/clear")
    def clear_logs():
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True:
            raise ValueError("Confirm clearing the log explicitly.")
        return jsonify(controller.clear_logs(g.principal.object_id))

    @application.post("/api/rebuild/run")
    def rebuild_run():
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True or not isinstance(body.get("operationId"), str):
            raise ValueError("Confirm the index rebuild explicitly.")
        mode = body.get("mode", "targeted")
        if mode not in MODES:
            raise ValueError("Choose a supported purge mode.")
        return jsonify(controller.start_rebuild(g.principal.object_id, identifier(body["operationId"]), mode)), 202

    @application.post("/api/indexers/<name>/actions")
    def indexer_action(name):
        body = request.get_json()
        if (
            not isinstance(body, dict) or body.get("confirmed") is not True
            or not isinstance(body.get("operationId"), str) or not isinstance(body.get("action"), str)
        ):
            raise ValueError("Confirm the indexing action explicitly.")
        return jsonify(controller.indexer_action(
            g.principal.object_id, identifier(body["operationId"]), name,
            body["action"], body.get("interval"),
        ))

    @application.post("/api/sources/remove")
    def remove_source():
        body = request.get_json()
        if (
            not isinstance(body, dict) or body.get("confirmed") is not True
            or not isinstance(body.get("operationId"), str) or not isinstance(body.get("sourceKey"), str)
        ):
            raise ValueError("Confirm the source removal explicitly.")
        return jsonify(controller.remove_source(g.principal.object_id, identifier(body["operationId"]), body["sourceKey"]))

    @application.post("/api/group-changes/preview")
    def preview_group():
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("share"), str) or not isinstance(body.get("groupId"), str):
            raise ValueError("A source share and existing group identifier are required.")
        return jsonify(controller.preview_group(g.principal.object_id, body["share"], body["groupId"]))

    @application.post("/api/group-changes/apply")
    def apply_group():
        body = request.get_json()
        if not isinstance(body, dict) or not all(isinstance(body.get(key), str) for key in ("draftId", "confirmation")):
            raise ValueError("A reviewed draft and typed maintenance confirmation are required.")
        return jsonify(controller.apply_group(
            g.principal.object_id, identifier(body["draftId"]), body["confirmation"],
        )), 202

    @application.post("/api/operations/<operation_id>/resume")
    def resume(operation_id):
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True:
            raise ValueError("Confirm the maintenance resume explicitly.")
        return jsonify(controller.retry(g.principal.object_id, identifier(operation_id))), 202

    @application.post("/api/operations/<operation_id>/cancel")
    def cancel(operation_id):
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True:
            raise ValueError("Confirm cancellation explicitly.")
        return jsonify(controller.cancel_unstarted(g.principal.object_id, identifier(operation_id)))

    @application.get("/api/integrations/mcp")
    def mcp_integration():
        return jsonify(integrations.mcp_status(gateway, settings))

    @application.post("/api/integrations/mcp/power")
    def mcp_power():
        body = request.get_json()
        if not isinstance(body, dict) or body.get("confirmed") is not True or body.get("action") not in integrations.POWER_ACTIONS:
            raise ValueError("Confirm starting or stopping the MCP server explicitly.")
        action = body["action"]
        integrations.set_mcp_power(gateway, settings, action)

        def change(state):
            audit(state, g.principal.object_id, f"mcp_{action}", "", settings.mcp_app)
            return {"action": action, "app": settings.mcp_app}
        return jsonify(store.update(change))

    @application.get("/api/integrations/foundry")
    def foundry_integration():
        return jsonify(integrations.foundry_status(gateway, settings))

    @application.get("/api/configuration")
    def configuration():
        return jsonify({
            "resourceGroup": settings.resource_group, "subscriptionId": settings.subscription,
            "searchService": settings.search_name, "searchApiVersion": settings.search_version,
            "knowledgeSource": settings.knowledge_source, "knowledgeBase": settings.knowledge_base,
            "filesAccount": settings.files_account, "foundryAccount": settings.foundry_account,
            "downloadApp": settings.download_app, "hostingPlan": settings.hosting_plan_id,
            "directoryLookupEnabled": settings.directory_enabled,
            "permissionMode": "source_group_mapping_not_ntfs", "consoleAdministratorCount": len(settings.administrators),
            "limits": [
                "Console administration does not grant document-content access.",
                "Permission changes require maintenance and full recreation of both application indexes.",
                "Chunks of deleted source files are removed only by a purge (scheduled or manual); incremental refreshes keep them.",
                "Existing SAS links can remain usable for five minutes; old conversations cannot be recalled.",
                "Storage connection secrets are read only from server configuration and sent directly to Azure; never to the browser or operation state.",
                "Directory lookup requires separately consented read-only Graph permissions.",
            ],
        })

    if start_worker:
        worker.start()
    return application
