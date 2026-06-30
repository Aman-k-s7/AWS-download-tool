"""
web_api_handle.py — Keycloak SSO + Gateway API integration

Multi-user architecture:
  1. User authenticates via Keycloak SSO (CentralIAM realm)
  2. Keycloak access token is used to call qualix commodity-config API
     through the Keycloak gateway (dev.perfeqtfoods.com/<service>/...)
  3. The API returns imageFolderName = S3 folder for that user's org
  4. Nothing is hardcoded — customer & S3 folder are resolved per-user

Access control: Only users with valid credentials in the Keycloak realm
can authenticate. The gateway validates the KC token and forwards requests.
"""

import requests
import os
import logging

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# ── Config helpers ────────────────────────────────────────────

def _get_keycloak_config() -> dict:
    return {
        "server_url":      os.getenv("KEYCLOAK_SERVER_URL",      ""),
        "realm":           os.getenv("KEYCLOAK_REALM",           "CentralIAM"),
        "client_id":       os.getenv("KEYCLOAK_CLIENT_ID",       ""),
        "gateway_url":     os.getenv("KEYCLOAK_GATEWAY_URL",     ""),
        "gateway_service": os.getenv("KEYCLOAK_GATEWAY_SERVICE", ""),
        "redirect_uri":    os.getenv("KEYCLOAK_REDIRECT_URI",    ""),
        "customer_claim":  os.getenv("KEYCLOAK_CUSTOMER_CLAIM",  ""),
    }


# ── Keycloak public config (for frontend) ────────────────────

def get_keycloak_public_config() -> dict:
    """Return Keycloak config safe to expose to the frontend."""
    kc = _get_keycloak_config()
    return {
        "url":         kc["server_url"],
        "realm":       kc["realm"],
        "clientId":    kc["client_id"],
        "redirectUri": kc["redirect_uri"],
    }


# ── Token exchange (authorization code → access token) ────────

def exchange_keycloak_code(code: str, redirect_uri: str) -> dict:
    """
    Exchange an authorization code for tokens via Keycloak's token endpoint.
    Server-to-server call (no browser involved).

    Returns dict: access_token, refresh_token, id_token, expires_in, etc.
    Raises ValueError on failure.
    """
    kc = _get_keycloak_config()
    token_url = f"{kc['server_url']}/realms/{kc['realm']}/protocol/openid-connect/token"

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": kc["client_id"],
        "redirect_uri": redirect_uri,
    }

    resp = requests.post(token_url, data=payload, timeout=15)
    if resp.status_code != 200:
        logger.error("Token exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise ValueError(f"Token exchange failed: {resp.status_code}")

    return resp.json()


# ── JWT validation ────────────────────────────────────────────

def verify_keycloak_token(token: str) -> dict:
    """
    Validate a Keycloak-issued JWT access token using JWKS.
    Returns decoded claims dict.
    Raises jwt.exceptions.InvalidTokenError on failure.
    """
    global _jwks_client
    kc = _get_keycloak_config()
    jwks_url = f"{kc['server_url']}/realms/{kc['realm']}/protocol/openid-connect/certs"

    if _jwks_client is None:
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)

    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={"verify_exp": True, "verify_aud": False},
        leeway=30,  # tolerate clock skew between local machine and Keycloak server
    )
    return claims


# ── Customer resolution from JWT claims ───────────────────────

def get_customer_from_claims(claims: dict) -> str:
    """
    Extract the customer/organization name from Keycloak JWT claims.

    Resolution priority:
      1. Custom claim configured in [KEYCLOAK] customer_claim
      2. Common claim names (customer_name, organization, etc.)
      3. Keycloak groups claim (first group name)
      4. Extract from preferred_username (e.g., abinbev.op1@agnext.in → abinbev)

    Returns customer name string.
    Raises ValueError if no customer can be determined.
    """
    # Log all claims (except sensitive ones) for debugging
    safe_claims = {k: v for k, v in claims.items() 
                   if k not in ('jti', 'exp', 'iat', 'auth_time')}
    logger.debug("[CUSTOMER EXTRACT DEBUG] JWT claims: %s", safe_claims)
    
    kc = _get_keycloak_config()

    # 1. Configurable claim name from config.INI [KEYCLOAK] customer_claim
    claim_key = kc.get("customer_claim", "").strip()
    if claim_key and claims.get(claim_key):
        logger.info("Customer from configured claim '%s': %s", claim_key, claims[claim_key])
        return claims[claim_key]

    # 2. Common claim names used in enterprise Keycloak setups
    for key in ("customer_name", "customer", "organization", "org_name",
                "clientName", "tenant", "company"):
        val = claims.get(key)
        if val and isinstance(val, str):
            logger.info("Customer from JWT claim '%s': %s", key, val)
            return val

    # 3. Keycloak groups claim (e.g., ["/ABInBev", "/OtherOrg"])
    groups = claims.get("groups", [])
    if groups and isinstance(groups, list):
        customer = groups[0].lstrip("/")
        logger.info("Customer from JWT groups: %s", customer)
        return customer

    # 4. Extract from email or preferred_username.
    #    e.g. "abinbev.op1@agnext.in" → "abinbev"
    #         "fw.admin@agnext.in"     → "fw"  (min length lowered to 2)
    #         "admin@agnext.in"        → "admin" (no dot in local part)
    for field in ("email", "preferred_username"):
        val = claims.get(field, "")
        if val and "@" in val:
            local_part = val.split("@")[0]
            if "." in local_part:
                org_candidate = local_part.split(".")[0]
                if len(org_candidate) >= 2:
                    logger.info("Customer inferred from %s '%s': %s", field, val, org_candidate)
                    return org_candidate
            elif len(local_part) >= 2:
                logger.info("Customer inferred from %s local part '%s': %s", field, val, local_part)
                return local_part

    # 5. Extract from sub claim (e.g. "f:uuid:fw.admin@agnext.in")
    sub = claims.get("sub", "")
    if ":" in sub:
        sub_parts = sub.split(":")
        sub_email = sub_parts[-1]
        if "@" in sub_email:
            local_part = sub_email.split("@")[0]
            org_candidate = local_part.split(".")[0] if "." in local_part else local_part
            if len(org_candidate) >= 2:
                logger.info("Customer inferred from sub '%s': %s", sub, org_candidate)
                return org_candidate

    # 6. Absolute last-resort — return whatever we can rather than crashing.
    #    The S3 folder is always resolved via the gateway afterward anyway.
    for field in ("name", "preferred_username", "email"):
        val = claims.get(field, "")
        if val:
            candidate = val.split("@")[0] if "@" in val else val   # strip domain
            candidate = candidate.split()[0] if " " in candidate else candidate  # first word
            if candidate:
                logger.warning(
                    "Customer using last-resort fallback from claim '%s': %s  "
                    "— set [KEYCLOAK] customer_claim in config.INI for a reliable mapping",
                    field, candidate,
                )
                return candidate

    # Nothing at all — log the full claim set and raise.
    logger.error(
        "Cannot determine customer from JWT claims. "
        "Available claims: %s",
        {k: v for k, v in claims.items() if k not in ('jti', 'exp', 'iat', 'auth_time')}
    )
    raise ValueError(
        "Cannot determine customer from JWT. "
        "Configure [KEYCLOAK] customer_claim in config.INI or add a "
        "customer/organization claim to the Keycloak client mapper."
    )


# ── S3 folder resolution via Gateway API ──────────────────────

def _gateway_get(access_token: str, endpoint: str, params: dict = None) -> dict:
    """
    Make a GET request to any endpoint via the Keycloak gateway.
    endpoint should NOT have a leading slash (e.g. 'api/user/keycloak-profile').
    Returns parsed JSON dict. Raises ValueError on HTTP/network errors.
    """
    kc = _get_keycloak_config()
    gateway_base = kc["gateway_url"].rstrip("/")
    service = kc["gateway_service"].strip("/")

    if not gateway_base or not service:
        raise ValueError("Gateway not configured. Set [KEYCLOAK] gateway_url and gateway_service.")

    url = f"{gateway_base}/api/asu/gateway/{service}/{endpoint}"
    logger.debug("[GATEWAY] GET %s params=%s", url, params)

    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        logger.debug("[GATEWAY] Response status=%s", resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        logger.info("[GATEWAY] Response body: %s", data)
        return data
    except requests.HTTPError as e:
        status = e.response.status_code
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        logger.warning("[GATEWAY] HTTP %s for %s — %s", status, url, body)
        if status in (401, 403):
            raise ValueError(
                f"Access denied (HTTP {status}): your account is not authorised to call {endpoint}."
            )
        raise ValueError(f"Gateway API error (HTTP {status}) for {endpoint}.")
    except ValueError:
        raise
    except Exception as e:
        logger.error("[GATEWAY] Request failed for %s", url, exc_info=True)
        raise ValueError(f"Cannot reach gateway API: {e}")


def fetch_user_profile_via_gateway(access_token: str) -> dict:
    """
    Call api/user/keycloak-profile via the gateway.
    Returns the full profile dict. Raises ValueError on failure.

    Expected to contain at minimum: customer_id
    """
    endpoint = os.getenv("API_USER_PROFILE_URL", "api/user/keycloak-profile")
    profile = _gateway_get(access_token, endpoint)
    logger.info("[USER PROFILE] Fetched profile: %s", profile)
    return profile


def fetch_client_meta_via_gateway(access_token: str, customer_id) -> str:
    """
    Call api/client-config/meta-data?customer_id=<id> via the gateway.
    Returns the S3 folder name (imageFolderName or equivalent).
    Raises ValueError if the field is missing or request fails.
    """
    endpoint = os.getenv("API_CLIENT_META_URL", "api/client-config/meta-data")
    data = _gateway_get(access_token, endpoint, params={"customer_id": customer_id})
    logger.info("[CLIENT META] Response for customer_id=%s: %s", customer_id, data)

    # Try common field names for the S3 folder
    for field in ("s3ImageFolderName", "imageFolderName", "image_folder_name",
                  "bucketFolder", "bucket_folder", "s3Folder", "s3_folder",
                  "folder_name", "folderName"):
        val = data.get(field)
        if val and isinstance(val, str):
            logger.info("[CLIENT META] S3 folder from field '%s': %s", field, val)
            return val

    logger.error(
        "[CLIENT META] Could not find S3 folder in response. "
        "Available fields: %s — set the correct field name in config [API_URI] client_meta_folder_field",
        list(data.keys()),
    )
    # Allow config override for the field name
    folder_field = os.getenv("API_CLIENT_META_FOLDER_FIELD", "")
    if folder_field and data.get(folder_field):
        logger.info("[CLIENT META] Using configured field '%s': %s", folder_field, data[folder_field])
        return str(data[folder_field])

    raise ValueError(
        f"client-config/meta-data response has no recognised folder field. "
        f"Available fields: {list(data.keys())}. "
        f"Set [API_URI] client_meta_folder_field in config.INI to the correct field name."
    )


def fetch_s3_client_via_gateway(access_token: str, customer_name: str) -> str:
    """
    Call the qualix commodity-config API via the Keycloak gateway to get
    the S3 imageFolderName for the logged-in user's organization.

    The gateway validates the Keycloak Bearer token AND checks that the
    user has valid qualix API credentials. If the user doesn't have qualix
    access, the gateway returns an error and we deny access.

    URL: <gateway_url>/<gateway_service>/<client_config_url><customer_name>
    Auth: Authorization: Bearer <keycloak_access_token>

    Returns imageFolderName from API.
    Raises ValueError if user is not authorized (no qualix access).
    """
    kc = _get_keycloak_config()

    s3_type = os.getenv("S3_TYPE", "iot")
    config_uri = os.getenv("API_CLIENT_CONFIG_URL", "portal/api/commodityconfig/v3/")
    customer_slug = customer_name.lower()

    # ── Call qualix API via Keycloak gateway ─────────────────
    gateway_base = kc["gateway_url"].rstrip("/")
    service = kc["gateway_service"].strip("/")

    if not gateway_base or not service:
        raise ValueError("Gateway not configured. Set [KEYCLOAK] gateway_url and gateway_service.")

    # URL pattern: <gateway_url>/api/asu/gateway/<service_name>/<qualix_endpoint>
    gateway_url = f"{gateway_base}/api/asu/gateway/{service}/{config_uri}{customer_slug}"
    
    logger.debug(
        "[GATEWAY DEBUG] Calling gateway: customer=%s customer_slug=%s url=%s service=%s",
        customer_name,
        customer_slug,
        gateway_url,
        service,
    )

    try:
        resp = requests.get(
            gateway_url,
            params={"type": s3_type},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        logger.debug("[GATEWAY DEBUG] Response status=%s", resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        logger.debug("[GATEWAY DEBUG] Response body: %s", data)
        s3_folder = data.get("imageFolderName") or ""
        if s3_folder:
            logger.info("[gateway] commodity-config OK → customer=%s imageFolderName=%s", customer_name, s3_folder)
            return s3_folder
        logger.warning("[gateway] Response missing imageFolderName for customer=%s: %s", customer_name, data)
        raise ValueError("Gateway returned no imageFolderName for this user.")

    except requests.HTTPError as e:
        status = e.response.status_code
        logger.warning("[GATEWAY DEBUG] HTTP error: status=%s url=%s customer=%s", status, gateway_url, customer_name)
        if status in (401, 403):
            logger.warning("User not authorized on qualix API (HTTP %s) customer=%s", status, customer_name)
            raise ValueError(
                "Access denied: your account does not have qualix API access. "
                "Only users with valid qualix credentials can use this tool."
            )
        logger.warning("[gateway] HTTP %s for %s customer=%s", status, gateway_url, customer_name)
        raise ValueError(
            f"Gateway API error (HTTP {status}). "
            "Contact admin to verify gateway route is configured."
        )
    except ValueError:
        raise
    except Exception as e:
        logger.error("[gateway] Request failed: %s", gateway_url, exc_info=True)
        raise ValueError(f"Cannot reach gateway API: {e}")
