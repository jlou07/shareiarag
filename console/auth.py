from dataclasses import dataclass

import jwt


class AuthenticationError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    object_id: str
    name: str


class TokenVerifier:
    def __init__(self, settings):
        self.settings = settings
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{settings.tenant}/discovery/v2.0/keys",
            cache_keys=True, lifespan=3600, timeout=10,
        )

    def verify(self, token: str) -> Principal:
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.settings.client_id,
                issuer=f"https://login.microsoftonline.com/{self.settings.tenant}/v2.0",
                options={"require": ["exp", "iat", "nbf", "oid", "tid", "aud", "iss"]},
                leeway=30,
            )
        except (jwt.PyJWTError, ValueError) as error:
            raise AuthenticationError("A valid console access token is required.") from error
        if claims["tid"] != self.settings.tenant or "Console.Access" not in claims.get("scp", "").split():
            raise AuthorizationError("The token does not grant access to this console.")
        if claims["oid"] not in self.settings.administrators:
            raise AuthorizationError("This account is not a console administrator.")
        return Principal(claims["oid"], str(claims.get("name", "Administrator"))[:120])

    def verify_search(self, token: str, principal: Principal):
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"],
                audience=["https://search.azure.com", "https://search.azure.com/"],
                issuer=[f"https://sts.windows.net/{self.settings.tenant}/",
                        f"https://login.microsoftonline.com/{self.settings.tenant}/v2.0"],
                options={"require": ["exp", "iat", "nbf", "oid", "tid", "aud", "iss"]},
                leeway=30,
            )
        except (jwt.PyJWTError, ValueError) as error:
            raise AuthenticationError("A valid delegated Search access token is required.") from error
        if (claims["tid"] != self.settings.tenant or claims["oid"] != principal.object_id
                or principal.object_id not in self.settings.administrators
                or "user_impersonation" not in claims.get("scp", "").split()
                or claims.get("azp", claims.get("appid")) != self.settings.client_id):
            raise AuthorizationError("Search access must be delegated by the signed-in console administrator.")
