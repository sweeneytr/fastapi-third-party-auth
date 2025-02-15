# -*- coding: utf-8 -*-
"""
Module for validating Open ID Connect tokens.

Usage
=====

.. code-block:: python3

    # This assumes you've already configured Auth in your_app/auth.py
    from your_app.auth import auth

    @app.get("/auth")
    def test_auth(authenticated_user: IDToken = Security(auth.required)):
        return f"Hello {authenticated_user.preferred_username}"
"""

from logging import getLogger
from typing import List
from typing import Optional
from typing import Type

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.openapi.models import OAuthFlowAuthorizationCode
from fastapi.openapi.models import OAuthFlowClientCredentials
from fastapi.openapi.models import OAuthFlowImplicit
from fastapi.openapi.models import OAuthFlowPassword
from fastapi.openapi.models import OAuthFlows
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security import HTTPBearer
from fastapi.security import OAuth2
from fastapi.security import SecurityScopes
from jose import ExpiredSignatureError
from jose import jwt
from jose.exceptions import JWTClaimsError, JWKError, JWTError, JWSError
from requests.exceptions import ConnectionError

from fastapi_third_party_auth import discovery
from fastapi_third_party_auth.grant_types import GrantType
from fastapi_third_party_auth.idtoken_types import IDToken

logger = getLogger(__name__)


class Auth(OAuth2):
    def __init__(
        self,
        openid_connect_url: str,
        issuer: Optional[str] = None,
        client_id: Optional[str] = None,
        scopes: List[str] = list(),
        grant_types: List[GrantType] = [GrantType.IMPLICIT],
        signature_cache_ttl: int = 3600,
        idtoken_model: Type[IDToken] = IDToken,
    ):
        """Configure authentication :func:`auth = Auth(...) <Auth>` and then:

        1. Show authentication in the interactive docs with :func:`Depends(auth) <Auth>`
           when setting up FastAPI.
        2. Use :func:`Security(auth.required) <Auth.required>` or
           :func:`Security(auth.optional) <Auth.optional>` in your endpoints to
           check user credentials.

        Args:
            openid_connect_url (URL): URL to the "well known" openid connect config
                e.g. https://dev-123456.okta.com/.well-known/openid-configuration
            issuer (URL): (Optional) The issuer URL from your auth server.
            client_id (str): (Optional) The client_id configured by your auth server.
            scopes (Dict[str, str]): (Optional) A dictionary of scopes and their descriptions.
            grant_types (List[GrantType]): (Optional) Grant types shown in docs.
            signature_cache_ttl (int): (Optional) How many seconds your app should
                cache the authorization server's public signatures.
            idtoken_model (Type): (Optional) The model to use for validating the ID Token.

        Raises:
            Nothing intentional
        """

        self.openid_connect_url = openid_connect_url
        self.issuer = issuer
        self.client_id = client_id
        self.idtoken_model = idtoken_model
        self.scopes = scopes
        
        self.discover = discovery.configure(cache_ttl=signature_cache_ttl)
        self.grant_types = grant_types

        try:
            flows = self.get_flows()
        except ConnectionError as e:
            logger.warning("Could not discover OIDC flows %s", e)
            flows = OAuthFlows()

        super().__init__(scheme_name="OIDC", flows=flows, auto_error=False)

    def get_flows(self) -> OAuthFlows:
        oidc_discoveries = self.discover.auth_server(
            openid_connect_url=self.openid_connect_url
        )
        # scopes_dict = {
        #     scope: "" for scope in self.discover.supported_scopes(oidc_discoveries)
        # }

        flows = OAuthFlows()
        if GrantType.AUTHORIZATION_CODE in self.grant_types:
            flows.authorizationCode = OAuthFlowAuthorizationCode(
                authorizationUrl=self.discover.authorization_url(oidc_discoveries),
                tokenUrl=self.discover.token_url(oidc_discoveries),
                # scopes=scopes_dict,
            )

        if GrantType.CLIENT_CREDENTIALS in self.grant_types:
            flows.clientCredentials = OAuthFlowClientCredentials(
                tokenUrl=self.discover.token_url(oidc_discoveries),
                # scopes=scopes_dict,
            )

        if GrantType.PASSWORD in self.grant_types:
            flows.password = OAuthFlowPassword(
                tokenUrl=self.discover.token_url(oidc_discoveries),
                # scopes=scopes_dict,
            )

        if GrantType.IMPLICIT in self.grant_types:
            flows.implicit = OAuthFlowImplicit(
                authorizationUrl=self.discover.authorization_url(oidc_discoveries),
                # scopes=scopes_dict,
            )
        
        return flows

    async def __call__(self, request: Request) -> None:
        return None

    def required(
        self,
        security_scopes: SecurityScopes,
        authorization_credentials: Optional[HTTPAuthorizationCredentials] = Depends(
            HTTPBearer()
        ),
    ) -> IDToken:
        """Validate and parse OIDC ID token against configuration.
        Note this function caches the signatures and algorithms of the issuing
        server for signature_cache_ttl seconds.

        Args:
            security_scopes (SecurityScopes): Security scopes
            auth_header (str): Base64 encoded OIDC Token. This is invoked
                behind the scenes by Depends.

        Return:
            IDToken (self.idtoken_model): User information

        raises:
            HTTPException(status_code=401, detail=f"Unauthorized: {err}")
            IDToken validation errors
        """

        id_token = self.authenticate_user(
            security_scopes,
            authorization_credentials,
            auto_error=True,
        )
        if id_token is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)
        else:
            return id_token

    def optional(
        self,
        security_scopes: SecurityScopes,
        authorization_credentials: Optional[HTTPAuthorizationCredentials] = Depends(
            HTTPBearer(auto_error=False)
        ),
    ) -> Optional[IDToken]:
        """Optionally validate and parse OIDC ID token against configuration.
        Will not raise if the user is not authenticated. Note this function
        caches the signatures and algorithms of the issuing server for
        signature_cache_ttl seconds.

        Args:
            security_scopes (SecurityScopes): Security scopes
            auth_header (str): Base64 encoded OIDC Token. This is invoked
                behind the scenes by Depends.

        Return:
            IDToken (self.idtoken_model): User information

        raises:
            IDToken validation errors
        """

        return self.authenticate_user(
            security_scopes,
            authorization_credentials,
            auto_error=False,
        )


    def _find_key(self, token: str) -> dict:
        oidc_discoveries = self.discover.auth_server(
            openid_connect_url=self.openid_connect_url
        )
        try:
            keys = self.discover.public_keys(oidc_discoveries)["keys"]
        except KeyError as e:
            raise JWKError("Badly formed JWKs_uri") from e

        header = jwt.get_unverified_header(token)
        try:
            kid = header['kid']
        except KeyError as e:
            raise JWTError("field 'kid' is missing from JWT headers") from e

        for key in keys:
            try:
                key_kid = key['kid']
            except KeyError as e:
                raise JWKError("field 'kid' is missing from JWK") from e
            if key_kid == kid:
                return key
        
        raise JWKError(f"Could not find JWK 'kid'={kid}")


    def authenticate_user(
        self,
        security_scopes: SecurityScopes,
        authorization_credentials: Optional[HTTPAuthorizationCredentials],
        auto_error: bool,
    ) -> Optional[IDToken]:
        """Validate and parse OIDC ID token against against configuration.
        Note this function caches the signatures and algorithms of the issuing server
        for signature_cache_ttl seconds.

        Args:
            security_scopes (SecurityScopes): Security scopes
            auth_header (str): Base64 encoded OIDC Token
            auto_error (bool): If True, will raise an HTTPException if the user
                is not authenticated.

        Return:
            IDToken (self.idtoken_model): User information

        raises:
            HTTPException(status_code=401, detail=f"Unauthorized: {err}")
        """

        if (
            authorization_credentials is None
            or authorization_credentials.scheme.lower() != "bearer"
        ):
            if auto_error:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
                )
            else:
                return None
        
        try:
            oidc_discoveries = self.discover.auth_server(
                openid_connect_url=self.openid_connect_url
            )
        except ConnectionError as e:
            logger.warning("Could not reach auth server %e", e)
            raise HTTPException(503, detail="Could not reach auth server") from e
        algorithms = self.discover.signing_algos(oidc_discoveries)
        key = self._find_key(authorization_credentials.credentials)

        try:
            id_token = jwt.decode(
                authorization_credentials.credentials,
                key,
                algorithms,
                issuer=self.issuer,
                audience=self.client_id,
                options={
                    # Disabled at_hash check since we aren't using the access token
                    "verify_at_hash": False,
                    "verify_iss": self.issuer is not None,
                    "verify_aud": self.client_id is not None,
                },
            )

            if (
                "aud" in id_token
                and type(id_token["aud"]) == list
                and len(id_token["aud"]) >= 1
                and "azp" not in id_token
            ):
                raise JWTError(
                    'Missing authorized party "azp" in IDToken when there '
                    "are multiple audiences"
                )

        except (ExpiredSignatureError, JWTError, JWTClaimsError) as error:
            raise HTTPException(status_code=401, detail=f"Unauthorized: {error}")

        expected_scopes = set(self.scopes + security_scopes.scopes)
        token_scopes = id_token.get("scope", "").split(" ")
        if not expected_scopes.issubset(token_scopes):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail=(
                    f"Missing scope token, expected {expected_scopes} to be a "
                    f"subset of received {token_scopes}",
                ),
            )

        return self.idtoken_model(**id_token)
