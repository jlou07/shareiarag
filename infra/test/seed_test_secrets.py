"""Seed test-only Key Vault secrets after the separate deployment approval."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

import bootstrap_search as test
from build_function_package import SOURCE_DIR, pin_test_identity
import testenv


DOWNLOAD_SECRET = "download-provider-secret"
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"


def files_secret(env):
    return f"files-connection-{env.files}"


def _token(subscription, resource):
    return test._az("account", "get-access-token", "--subscription", subscription,
                    "--resource", resource, "--query", "accessToken", "-o", "tsv")


def _request(method, url, token, body=None, allow_missing=False, read_json=False):
    headers = {"Authorization": f"Bearer {token}"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    try:
        with build_opener(test._NoRedirect).open(Request(url, data=payload, headers=headers, method=method), timeout=60) as response:
            return json.loads(response.read()) if read_json else response.status
    except HTTPError as error:
        if allow_missing and error.code == 404:
            return 404
        raise RuntimeError(f"Azure {method} request failed with HTTP {error.code}; inspect the test resource.") from None
    except (URLError, OSError, ValueError) as error:
        raise RuntimeError("Could not complete or parse the Azure request.") from error


def _vault_url(env, name):
    if name not in (files_secret(env), DOWNLOAD_SECRET):
        raise ValueError("Unexpected test secret name.")
    return f"https://{env.vault}.vault.azure.net/secrets/{name}?api-version=7.4"


def _ensure_secret_missing(env, name, token):
    if _request("GET", _vault_url(env, name), token, allow_missing=True) != 404:
        raise RuntimeError(f"Test secret {name} already exists; it will not be overwritten.")


def seed_files_secret(env):
    subscription, _ = test.verify_test_scope(env)
    name = files_secret(env)
    vault_token = _token(subscription, "https://vault.azure.net")
    _ensure_secret_missing(env, name, vault_token)
    key = test._az("storage", "account", "keys", "list", "--subscription", subscription,
                   "--resource-group", env.resource_group, "--account-name", env.files,
                   "--query", "[0].value", "-o", "tsv")
    connection = (f"DefaultEndpointsProtocol=https;AccountName={env.files};"
                  f"AccountKey={key};EndpointSuffix=core.windows.net")
    test.validate_connection(env, connection)
    try:
        _request("PUT", _vault_url(env, name), vault_token,
                 {"value": connection, "contentType": "Azure Storage connection string"})
    except RuntimeError as error:
        raise RuntimeError(
            f"The test Files secret write may have succeeded. Inspect {name} in {env.vault} "
            "without revealing its value; reconcile any version before retrying."
        ) from error


def seed_download_secret(env):
    try:
        client_id = str(UUID(env.download_client_id))
    except (ValueError, AttributeError) as error:
        raise ValueError("The test download client ID must be a UUID.") from error
    subscription, tenant = test.verify_test_scope(env)
    pin_test_identity((SOURCE_DIR / "function_app.py").read_text(encoding="utf-8"), client_id, tenant)
    app = json.loads(test._az(
        "ad", "app", "show", "--id", client_id,
        "--query", "{id:id,appId:appId,displayName:displayName,passwordNames:passwordCredentials[].displayName}",
        "-o", "json",
    ))
    if app.get("appId", "").lower() != client_id or app.get("displayName") != env.download_app:
        raise RuntimeError("The client ID does not identify the expected test Entra application.")
    if env.easyauth_credential in (app.get("passwordNames") or []):
        raise RuntimeError("A test credential with this name already exists; inspect it before retrying.")
    try:
        app_object_id = str(UUID(app["id"]))
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError("The test Entra application has no valid object ID.") from error

    function_id = test.resource_id(env, subscription, "Microsoft.Web/sites", env.download_site)
    auth_uri = f"https://management.azure.com{function_id}/config/authsettingsV2?api-version=2024-11-01"
    configured = test._az(
        "rest", "--method", "get", "--uri", auth_uri, "--subscription", subscription,
        "--query", "properties.identityProviders.azureActiveDirectory.registration.clientId", "-o", "tsv",
    )
    if configured.lower() != client_id:
        raise RuntimeError("The test Function is not configured for this test Entra application.")

    vault_token = _token(subscription, "https://vault.azure.net")
    _ensure_secret_missing(env, DOWNLOAD_SECRET, vault_token)
    graph_token = _token(subscription, "https://graph.microsoft.com")
    graph_app = f"{GRAPH_ENDPOINT}/applications/{app_object_id}"
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    try:
        credential = _request("POST", f"{graph_app}/addPassword", graph_token,
                              {"passwordCredential": {"displayName": env.easyauth_credential, "endDateTime": expiry}},
                              read_json=True)
    except RuntimeError as error:
        raise RuntimeError(
            f"Graph may have created credential {env.easyauth_credential} on {env.download_app}. "
            "Inspect and reconcile that test application before retrying."
        ) from error
    try:
        key_id = str(UUID(credential["keyId"]))
        secret = credential["secretText"]
        if not isinstance(secret, str) or not secret:
            raise ValueError("Missing secret text.")
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError("A test credential may have been created; inspect the test application before retrying.") from error
    try:
        _request("PUT", _vault_url(env, DOWNLOAD_SECRET), vault_token,
                 {"value": secret, "contentType": "Test download application credential"})
    except RuntimeError as error:
        try:
            _request("POST", f"{graph_app}/removePassword", graph_token, {"keyId": key_id})
        except RuntimeError as rollback_error:
            raise RuntimeError(
                f"The vault write and credential rollback have uncertain outcomes. Inspect {DOWNLOAD_SECRET} "
                f"in {env.vault} and test credential {key_id}; reconcile both before retrying."
            ) from rollback_error
        raise RuntimeError(
            f"The newly created test credential was removed, but the vault write may have succeeded. "
            f"Inspect and reconcile {DOWNLOAD_SECRET} in {env.vault} before retrying."
        ) from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("secret", choices=("files", "download"))
    parser.add_argument("--params", help=f"Parameters file (default: ${testenv.PARAMS_ENV} or runtime.parameters.json).")
    parser.add_argument("--execute", action="store_true", help="Create the selected secret after approval.")
    args = parser.parse_args()
    env = testenv.load(args.params)
    if not args.execute:
        print(f"Preview only: would create the {args.secret} secret in {env.vault}.")
        return
    print("The secret-existence check is not atomic. Do not initialize the same vault from another session.")
    test.confirm(env, "create the test secret")
    if args.secret == "files":
        seed_files_secret(env)
    else:
        seed_download_secret(env)
    print(f"Created the {args.secret} secret in the test vault; no secret value was displayed.")


if __name__ == "__main__":
    main()
