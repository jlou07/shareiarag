"""Authenticated ASGI entry point for the hosted MCP server."""

import asyncio
import logging
import os
from uuid import UUID

import jwt
import msal
from jwt import PyJWKClient
from starlette.requests import Request
from starlette.responses import JSONResponse

import server

if not server.HOSTED:
    raise RuntimeError("Set MCP_MODE=hosted before starting cloud_app.")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured.")
    return value


TENANT_ID = str(UUID(_required("AZURE_TENANT_ID")))
CLIENT_ID = str(UUID(_required("MCP_CLIENT_ID")))
FIC_IDENTITY_CLIENT_ID = os.environ.get("MCP_FIC_IDENTITY_CLIENT_ID", "").strip()
if FIC_IDENTITY_CLIENT_ID:
    # Secretless: a user-assigned managed identity signs a federated client assertion for the app.
    from azure.identity import ManagedIdentityCredential

    _fic_credential = ManagedIdentityCredential(client_id=str(UUID(FIC_IDENTITY_CLIENT_ID)))

    def _client_assertion() -> str:
        return _fic_credential.get_token("api://AzureADTokenExchange/.default").token

    CLIENT_CREDENTIAL = {"client_assertion": _client_assertion}
else:
    CLIENT_CREDENTIAL = _required("MCP_CLIENT_SECRET")
    if CLIENT_CREDENTIAL.startswith("@Microsoft.KeyVault("):
        raise RuntimeError("The MCP client credential Key Vault reference is unresolved.")
AUDIENCES = [value.strip() for value in _required("MCP_TOKEN_AUDIENCES").split(",") if value.strip()]
ALLOWED_CLIENTS = {
    str(UUID(value.strip()))
    for value in _required("MCP_ALLOWED_CLIENT_IDS").split(",")
    if value.strip()
}
REQUIRED_SCOPE = _required("MCP_REQUIRED_SCOPE")
if not AUDIENCES or not ALLOWED_CLIENTS:
    raise RuntimeError("Token audiences and allowed client IDs must not be empty.")

ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
SIGNING_KEYS = PyJWKClient(
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys",
    cache_keys=True, lifespan=3600, timeout=10,
)


class ForbiddenToken(Exception):
    pass


class DelegationError(Exception):
    pass


def _verify_and_exchange(assertion: str) -> str:
    key = SIGNING_KEYS.get_signing_key_from_jwt(assertion)
    claims = jwt.decode(
        assertion,
        key.key,
        algorithms=["RS256"],
        audience=AUDIENCES,
        issuer=ISSUER,
        options={"require": ["exp", "iat", "nbf", "oid", "tid", "aud", "iss", "scp", "azp"]},
        leeway=30,
    )
    if (
        claims["tid"] != TENANT_ID
        or not isinstance(claims["oid"], str)
        or not claims["oid"]
        or not isinstance(claims["scp"], str)
        or REQUIRED_SCOPE not in claims["scp"].split()
        or claims["azp"] not in ALLOWED_CLIENTS
    ):
        raise ForbiddenToken("The token does not grant delegated access.")

    client = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_CREDENTIAL,
    )
    result = client.acquire_token_on_behalf_of(
        user_assertion=assertion,
        scopes=["https://search.azure.com/.default"],
    )
    token = result.get("access_token")
    if not token:
        logging.warning("Delegation to Azure AI Search failed: %s", result.get("error", "unknown_error"))
        raise DelegationError("Delegated access to Azure AI Search failed.")
    return token


class UserAuthentication:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        authorization = request.headers.get("authorization", "")
        scheme, separator, assertion = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not assertion.strip():
            response = JSONResponse(
                {"error": "A delegated access token is required."}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            try:
                token = await asyncio.to_thread(_verify_and_exchange, assertion.strip())
            except jwt.PyJWKClientConnectionError:
                logging.error("Unable to retrieve Entra token signing keys.")
                response = JSONResponse({"error": "Authentication service unavailable."}, status_code=503)
            except jwt.PyJWTError:
                response = JSONResponse(
                    {"error": "Invalid access token."}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            except ForbiddenToken:
                response = JSONResponse({"error": "Access denied."}, status_code=403)
            except DelegationError:
                response = JSONResponse(
                    {"error": "Unable to obtain delegated Search access."}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            else:
                scope.setdefault("state", {})["search_token"] = token
                await self.app(scope, receive, send)
                return
        await response(scope, receive, send)


@server.mcp.custom_route("/healthz", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = UserAuthentication(server.mcp.streamable_http_app())
