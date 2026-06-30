import os
import io
import json
import datetime
import zipfile
import uuid
import logging
import pathlib
import time
from logging.handlers import RotatingFileHandler

import boto3
from botocore.config import Config as BotoConfig

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

from web_api_handle import verify_keycloak_token, fetch_s3_client_via_gateway, get_keycloak_public_config, get_customer_from_claims, exchange_keycloak_code, fetch_user_profile_via_gateway, fetch_client_meta_via_gateway

from dotenv import load_dotenv
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Logging setup ──────────────────────────────────────────────

def _setup_logging() -> str:
    """Configure console + rotating-file logging from LOGFILE_PATH in env."""
    raw_path = os.getenv("LOGFILE_PATH", "logs/app.log")
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(BASE_DIR, raw_path)
    pathlib.Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(raw_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)

    for noisy in ("boto3", "botocore", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return raw_path

_log_path = _setup_logging()
# ──────────────────────────────────────────────────────────────

app = FastAPI(title="AgNext S3 Download Manager", version="3.0")


@app.middleware("http")
async def _request_logger(request: Request, call_next):
    """Log every HTTP request with method, path, status, and elapsed time."""
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000
    logging.getLogger("agnext.http").info(
        "HTTP %-6s %-40s → %s  %.0fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# Per-user S3 config cache: keycloak sub → config dict
user_cache: dict = {}

# Prepared ZIP store: download_token → BytesIO (cleared after served)
zip_store: dict = {}

logger = logging.getLogger(__name__)
logger.info("=" * 55)
logger.info("AgNext S3 Download Manager v3.0")
logger.info("Log    : %s", _log_path)
logger.info("=" * 55)


def _read_s3_config():
    """Read S3 settings from env."""
    return {
        "bucket": os.getenv("S3_BUCKET", "agnext-cognito"),
        "folder": os.getenv("S3_BUCKET_FOLDER", "visio_desktop/"),
        "region": os.getenv("S3_REGION", "us-east-2"),
        "pool":   os.getenv("S3_POOL_ID", ""),
        "client": os.getenv("S3_CLIENT", "").strip(),
        "type":   os.getenv("S3_TYPE", "iot"),
    }


def _get_session(request: Request) -> Optional[dict]:
    """
    Validate the Keycloak Bearer JWT from the Authorization header.
    Returns the cached user config dict, or None on failure.
    """
    auth = request.headers.get("Authorization", "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        claims = verify_keycloak_token(token)
        sub = claims.get("sub")
        if not sub:
            return None
        if sub not in user_cache:
            _populate_user_cache(sub, token, claims)
            username = claims.get("preferred_username") or claims.get("email") or sub
            logger.info("[LOGIN] Client successfully logged in: username=%s sub=%s", username, sub)
        return user_cache.get(sub)
    except ValueError as e:
        logger.warning("Access denied: %s", e)
        return None
    except Exception:
        logger.warning("JWT validation failed", exc_info=True)
        return None


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: validate Bearer JWT and return user config. Raises 401 on failure."""
    cfg = _get_session(request)
    if not cfg:
        raise HTTPException(status_code=401, detail="Unauthorized. Please log in.")
    return cfg


def _populate_user_cache(sub: str, token: str, claims: dict) -> None:
    """Fetch S3 config via gateway and cache it for this Keycloak user."""
    s3_cfg = _read_s3_config()
    logger.debug("S3 config loaded: bucket=%s region=%s", s3_cfg.get("bucket"), s3_cfg.get("region"))

    # ── Step 1: fetch user profile to get customer_id ─────────
    logger.info("[USER PROFILE] Fetching keycloak profile for sub=%s", sub)
    try:
        profile = fetch_user_profile_via_gateway(token)
        # customer_id may be top-level or nested inside a 'user' object
        user_obj = profile.get("user") or {}
        if isinstance(user_obj, str):
            user_obj = {}
        customer_id = (
            profile.get("customer_id") or profile.get("customerId")
            or user_obj.get("customer_id") or user_obj.get("customerId")
        )
        customer_name = (
            profile.get("customer_name") or profile.get("customerName")
            or profile.get("organization")
            or user_obj.get("customer_name") or user_obj.get("customerName")
            or user_obj.get("organization")
            or str(customer_id or "")
        )
        logger.info("[USER PROFILE] top-level keys=%s user keys=%s", list(profile.keys()), list(user_obj.keys()) if user_obj else [])
        if not customer_id:
            raise ValueError(
                f"keycloak-profile response has no customer_id. "
                f"Top-level fields: {list(profile.keys())} | user fields: {list(user_obj.keys())}"
            )
        logger.info("[USER PROFILE] customer_id=%s customer_name=%s", customer_id, customer_name)
    except ValueError as exc:
        # Fallback: derive customer name from JWT claims (old behaviour)
        logger.warning("[USER PROFILE FALLBACK] Profile API failed (%s); deriving customer from JWT claims", exc)
        customer_id = None
        customer_name = get_customer_from_claims(claims)
        logger.info("[CUSTOMER EXTRACTED] sub=%s customer_name=%s", sub, customer_name)

    # ── Step 2: fetch client metadata to get S3 folder ────────
    s3_client = None
    if customer_id is not None:
        logger.info("[CLIENT META] Fetching client config for customer_id=%s", customer_id)
        try:
            s3_client = fetch_client_meta_via_gateway(token, customer_id)
            logger.info("[CLIENT META] S3 folder=%s for customer_id=%s", s3_client, customer_id)
        except ValueError as exc:
            logger.warning("[CLIENT META FALLBACK] Metadata API failed (%s); will use fallback", exc)

    # If metadata API failed, fall back to old qualix API (or hardcoded config)
    if not s3_client:
        try:
            s3_client = fetch_s3_client_via_gateway(token, customer_name)
            logger.info("[GATEWAY SUCCESS] Retrieved S3 folder=%s for customer=%s", s3_client, customer_name)
        except ValueError as exc:
            fallback_client = s3_cfg.get("client", "")
            error_text = str(exc)
            can_fallback = fallback_client and (
                "Gateway API error" in error_text
                or "Cannot reach gateway API" in error_text
                or "Gateway returned no imageFolderName" in error_text
            )
            if not can_fallback:
                logger.error("[GATEWAY FATAL] Cannot determine S3 folder for customer=%s. Error: %s", customer_name, error_text)
                raise
            logger.warning(
                "[GATEWAY FALLBACK] All lookups failed for customer=%s; using hardcoded fallback=%s",
                customer_name, fallback_client,
            )
            s3_client = fallback_client

    display_name = (
        claims.get("name")
        or claims.get("given_name")
        or customer_name
    )
    user_cache[sub] = {
        "id":         s3_client,
        "name":       customer_name,
        "first_name": display_name,
        "bucket":     s3_cfg["bucket"],
        "folder":     s3_cfg["folder"],
        "region":     s3_cfg["region"],
        "pool":       s3_cfg["pool"],
        "type":       s3_cfg["type"],
        "sub":        sub,
    }
    resolved_prefix = f"{s3_cfg['folder'].rstrip('/')}/{s3_client.strip('/')}/"
    resolved_uri = f"s3://{s3_cfg['bucket']}/{resolved_prefix}"
    logger.info("[S3 FINAL PATH] bucket=%s prefix=%s uri=%s",
                s3_cfg["bucket"], resolved_prefix, resolved_uri)
    logger.info("[CACHE POPULATED] sub=%s customer=%s s3_folder=%s display_name=%s",
                sub, customer_name, s3_client, display_name)


def _get_s3_resource(client_cfg):
    """Get authenticated S3 resource using Cognito Identity Pool."""
    boto_cfg = BotoConfig(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2})
    cognito = boto3.client("cognito-identity", region_name=client_cfg["region"], config=boto_cfg)
    identity = cognito.get_id(IdentityPoolId=client_cfg["pool"])
    creds = cognito.get_credentials_for_identity(IdentityId=identity["IdentityId"])

    return boto3.resource(
        "s3",
        aws_access_key_id=creds["Credentials"]["AccessKeyId"],
        aws_secret_access_key=creds["Credentials"]["SecretKey"],
        aws_session_token=creds["Credentials"]["SessionToken"],
        region_name=client_cfg["region"],
        config=boto_cfg,
    )

@app.get("/")
async def index():
    logger.info("API request: GET /")
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.get("/silent-check-sso.html")
async def silent_check_sso():
    logger.info("API request: GET /silent-check-sso.html")
    return FileResponse(os.path.join(BASE_DIR, "silent-check-sso.html"))

@app.get("/api/kc-config")
async def api_kc_config():
    """Return Keycloak configuration for the frontend (no secrets)."""
    cfg = get_keycloak_public_config()
    logger.info(
        "API request: GET /api/kc-config realm=%s client_id=%s",
        cfg.get("realm"),
        cfg.get("clientId"),
    )
    return cfg


class TokenExchangeRequest(BaseModel):
    code: str
    redirect_uri: str


@app.post("/api/token-exchange")
async def api_token_exchange(body: TokenExchangeRequest):
    """Exchange Keycloak authorization code for tokens (server-side)."""
    logger.info("API request: POST /api/token-exchange redirect_uri=%s", body.redirect_uri)
    if not body.code.strip():
        logger.warning("Token exchange rejected: missing authorization code")
        raise HTTPException(status_code=400, detail="Missing authorization code")
    if not body.redirect_uri.strip():
        logger.warning("Token exchange rejected: missing redirect_uri")
        raise HTTPException(status_code=400, detail="Missing redirect_uri")

    try:
        tokens = exchange_keycloak_code(body.code.strip(), body.redirect_uri.strip())
        logger.info("Token exchange success: access_token_issued=True")
        return tokens
    except ValueError as e:
        logger.warning("Token exchange failed: %s", e)
        raise HTTPException(status_code=401, detail=str(e))
    except Exception:
        logger.exception("Token exchange error")
        raise HTTPException(status_code=500, detail="Token exchange failed")


# ── Session check ─────────────────────────────────────────

# /api/me is not used by the frontend — commented out intentionally
# @app.get("/api/me")
# async def api_me(cfg: dict = Depends(get_current_user)):
#     username = cfg.get("first_name") or cfg.get("name") or cfg.get("sub")
#     return {
#         "valid": True,
#         "client": {
#             "name":       cfg["name"],
#             "first_name": cfg["first_name"],
#             "id":         cfg["id"],
#             "type":       cfg["type"],
#         },
#     }


# ── Logout

@app.post("/api/logout")
async def api_logout(request: Request):
    """Clear server-side user cache for the calling user."""
    logger.info("API request: POST /api/logout")
    auth = request.headers.get("Authorization", "").strip()
    if auth.startswith("Bearer "):
        try:
            claims = verify_keycloak_token(auth[7:])
            sub = claims.get("sub")
            if sub:
                user_cache.pop(sub, None)
                logger.info("User cache cleared on logout — sub=%s", sub)
        except Exception:
            pass
    else:
        logger.warning("Logout called without Bearer token")
    return {"ok": True}


# ── List Files 

@app.get("/api/files")
async def api_list_files(
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query("", description="YYYY-MM-DD"),
    filter: str = Query("", description="File extension filter e.g. .json"),
    cfg: dict = Depends(get_current_user),
):
    """List S3 sample folders within date range + data_collection folders."""
    logger.info(
        "API request: GET /api/files sub=%s customer=%s bucket=%s start_date=%s end_date=%s filter=%s",
        cfg.get("sub"),
        cfg.get("name"),
        cfg.get("bucket"),
        start_date,
        end_date,
        filter,
    )
    filter_ext = filter.strip().lower()
    if filter_ext and not filter_ext.startswith("."):
        filter_ext = "." + filter_ext

    IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    if end_date:
        end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc
        )
    else:
        end_dt = start_dt.replace(hour=23, minute=59, second=59)

    start_epoch_ms = int((start_dt - IST_OFFSET).timestamp() * 1000)
    end_epoch_ms = int((end_dt - IST_OFFSET).timestamp() * 1000)

    prefix = f"{cfg['folder'].rstrip('/')}/{cfg['id'].strip('/')}/"

    logger.info("[FILES] Scanning bucket=%s prefix=%s date_range=%s to %s",
                cfg["bucket"], prefix, start_date, end_date)

    s3 = _get_s3_resource(cfg)
    bucket = s3.Bucket(cfg["bucket"])
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=s3.meta.client._request_signer._credentials.access_key,
        aws_secret_access_key=s3.meta.client._request_signer._credentials.secret_key,
        aws_session_token=s3.meta.client._request_signer._credentials.token,
        region_name=cfg["region"],
    )

    # Step 1: List only top-level epoch folders (using delimiter) to narrow scope
    matching_prefixes = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            folder_prefix = cp["Prefix"]  # e.g. visio_desktop/client/1716970000000/
            folder_name = folder_prefix[len(prefix):].rstrip("/")
            epoch_text = folder_name.split("_", 1)[0] if "_" in folder_name else folder_name
            if epoch_text.isdigit() and len(epoch_text) >= 10:
                epoch_ms = int(epoch_text)
                if start_epoch_ms <= epoch_ms <= end_epoch_ms:
                    matching_prefixes.append((folder_prefix, folder_name, epoch_ms))

    # Return folders (not individual files)
    folders = []
    for folder_prefix, folder_name, epoch_ms in matching_prefixes:
        epoch_s = epoch_ms / 1000.0 if epoch_ms > 1e12 else float(epoch_ms)
        try:
            dt = datetime.datetime.fromtimestamp(epoch_s, tz=datetime.timezone.utc)
            date_display = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_display = ""
        folders.append({
            "key":  folder_prefix,
            "name": folder_name,
            "date": date_display,
            "source": "samples",
        })

    # Also scan data_collection folder — parallel to samples.
    # Structure: visio_desktop/{client}/data_collection/{commodity}/{subfolder}/
    # First list commodity folders, then list subfolders within each so users
    # can select specific subfolders instead of downloading entire commodities.
    dc_prefix = f"{cfg['folder'].rstrip('/')}/{cfg['id'].strip('/')}/data_collection/"
    dc_folders = []
    try:
        # Level 1: commodity folders (barley, wheat, etc.)
        commodity_prefixes = []
        for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=dc_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                commodity_prefixes.append(cp["Prefix"])

        # Level 2: subfolders inside each commodity
        for commodity_path in commodity_prefixes:
            commodity_name = commodity_path[len(dc_prefix):].rstrip("/")
            has_subfolders = False
            for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=commodity_path, Delimiter="/"):
                for cp in page.get("CommonPrefixes", []):
                    subfolder_path = cp["Prefix"]
                    subfolder_name = subfolder_path[len(commodity_path):].rstrip("/")
                    has_subfolders = True
                    dc_folders.append({
                        "key":  subfolder_path,
                        "name": f"{commodity_name}/{subfolder_name}",
                        "date": "",
                        "source": "data_collection",
                    })
            # If no subfolders, list the commodity folder itself (has loose files)
            if not has_subfolders:
                dc_folders.append({
                    "key":  commodity_path,
                    "name": commodity_name,
                    "date": "",
                    "source": "data_collection",
                })
    except Exception as exc:
        logger.warning("Failed to scan data_collection prefix %s: %s", dc_prefix, exc)

    dc_folders.sort(key=lambda f: f["name"])
    folders.sort(key=lambda f: f["name"], reverse=True)
    logger.info(
        "API response: GET /api/files sample_folders=%s data_collection_folders=%s",
        len(folders),
        len(dc_folders),
    )
    return {
        "files": folders,
        "total": len(folders),
        "data_collection": dc_folders,
        "dc_total": len(dc_folders),
    }


@app.get("/api/dc-expand")
async def api_dc_expand(
    prefix: str = Query(..., description="S3 prefix to expand"),
    cfg: dict = Depends(get_current_user),
):
    """Expand a data_collection subfolder to show sub-subfolders + files."""
    logger.info(
        "API request: GET /api/dc-expand sub=%s prefix=%s",
        cfg.get("sub"),
        prefix,
    )
    prefix_to_expand = prefix.strip()
    if not prefix_to_expand:
        raise HTTPException(status_code=400, detail="prefix parameter is required.")

    # Security: verify prefix belongs to this user's path
    user_dc_prefix = f"{cfg['folder'].rstrip('/')}/{cfg['id'].strip('/')}/data_collection/"
    if not prefix_to_expand.startswith(user_dc_prefix):
        raise HTTPException(status_code=403, detail="Invalid prefix.")

    if not prefix_to_expand.endswith("/"):
        prefix_to_expand += "/"

    try:
        s3 = _get_s3_resource(cfg)
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=s3.meta.client._request_signer._credentials.access_key,
            aws_secret_access_key=s3.meta.client._request_signer._credentials.secret_key,
            aws_session_token=s3.meta.client._request_signer._credentials.token,
            region_name=cfg["region"],
        )
        paginator = s3_client.get_paginator("list_objects_v2")

        subfolders = []
        files = []

        for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=prefix_to_expand, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                sub_path = cp["Prefix"]
                sub_name = sub_path[len(prefix_to_expand):].rstrip("/")
                subfolders.append({"key": sub_path, "name": sub_name, "type": "folder"})
            for obj in page.get("Contents", []):
                if obj["Key"] == prefix_to_expand:
                    continue
                filename = obj["Key"][len(prefix_to_expand):]
                if "/" in filename:
                    continue
                files.append({
                    "key": obj["Key"],
                    "name": filename,
                    "size": obj["Size"],
                    "type": "file",
                })

        logger.info(
            "API response: GET /api/dc-expand subfolders=%s files=%s",
            len(subfolders),
            len(files),
        )
        return {"subfolders": subfolders, "files": files, "total": len(subfolders) + len(files)}
    except Exception as exc:
        logger.exception("Failed to expand DC prefix %s", prefix_to_expand)
        raise HTTPException(status_code=502, detail="Failed to list contents.")


# ── Download: SSE progress while packaging ───────────────

@app.get("/api/download/prepare")
async def api_download_prepare(
    request: Request,
    keys: str = Query("", description="Comma-separated S3 keys"),
    organize_by_type: str = Query("0"),
    file_types: str = Query(""),
):
    """
    Packages selected S3 files into a ZIP while streaming SSE progress events.
    On completion emits: {"type": "ready", "token": "<download_token>"}
    """
    logger.info(
        "API request: GET /api/download/prepare keys_count=%s organize_by_type=%s file_types=%s",
        len([k for k in keys.split(",") if k.strip()]),
        organize_by_type,
        file_types,
    )
    user_cfg = _get_session(request)
    if not user_cfg:
        logger.warning("Download prepare unauthorized request")
        return StreamingResponse(
            iter(['data: {"type":"error","msg":"Unauthorized"}\n\n']),
            media_type="text/event-stream",
        )

    keys_list = [k.strip() for k in keys.split(",") if k.strip()]
    if not keys_list:
        logger.warning("Download prepare rejected: no keys selected")
        return StreamingResponse(
            iter(['data: {"type":"error","msg":"No files selected"}\n\n']),
            media_type="text/event-stream",
        )

    do_organize = organize_by_type == "1"
    allowed_types = set(file_types.strip().split(",")) if file_types.strip() else set()
    cfg = user_cfg
    logger.info(
        "Download prepare authorized: sub=%s customer=%s s3_folder=%s",
        cfg.get("sub"),
        cfg.get("name"),
        cfg.get("id"),
    )

    def generate():
        prefix = f"{cfg['folder'].rstrip('/')}/{cfg['id'].strip('/')}/"
        dc_prefix = f"{cfg['folder'].rstrip('/')}/{cfg['id'].strip('/')}/data_collection/"
        done = 0
        failed = 0

        def _get_type_folder(filename):
            ext = os.path.splitext(filename)[1].lower().lstrip(".")
            type_map = {
                "jpg": "images", "jpeg": "images", "png": "images",
                "gif": "images", "bmp": "images", "tiff": "images", "webp": "images",
                "csv": "csv", "xlsx": "excel", "xls": "excel",
                "json": "json", "xml": "xml",
                "pdf": "pdf", "doc": "documents", "docx": "documents",
                "mp4": "videos", "avi": "videos", "mov": "videos",
                "txt": "text", "log": "text",
            }
            return type_map.get(ext, ext if ext else "other")

        def _file_passes_type_filter(filename):
            """Check if file matches the selected type filters."""
            if not allowed_types:
                return True  # No filter = include all
            file_type = _get_type_folder(filename)
            return file_type in allowed_types

        yield f"data: {json.dumps({'type':'log','cls':'info','msg':f'[INFO] Connecting to S3...'})}\n\n"

        try:
            s3 = _get_s3_resource(cfg)
            bucket = s3.Bucket(cfg["bucket"])
        except Exception as exc:
            yield f"data: {json.dumps({'type':'error','msg':f'[ERROR] Auth failed: {exc}'})}\n\n"
            return

        # First pass: count total files across all selected folders
        all_folder_files = []
        for key in keys_list:
            # Support both sample and data_collection prefixes
            # Check dc_prefix first since it's a subdirectory of prefix
            if not key.startswith(dc_prefix) and not key.startswith(prefix):
                failed += 1
                continue
            folder_files = [
                obj for obj in bucket.objects.filter(Prefix=key)
                if not obj.key.endswith("/")
            ]
            # Apply file type filter if specified
            if allowed_types:
                folder_files = [obj for obj in folder_files if _file_passes_type_filter(obj.key.split("/")[-1])]
            all_folder_files.append((key, folder_files))

        total = sum(len(files) for _, files in all_folder_files) + failed

        yield f"data: {json.dumps({'type':'log','cls':'info','msg':f'[INFO] Packaging {len(keys_list)} folder(s), {total} file(s) into ZIP...'})}\n\n"
        logger.info(
            "Download packaging started: selected_folders=%s total_files=%s organize_by_type=%s",
            len(keys_list),
            total,
            do_organize,
        )

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for key, folder_files in all_folder_files:
                # Determine if this is a data_collection folder
                is_dc = key.startswith(dc_prefix)
                base_prefix = dc_prefix if is_dc else prefix

                for obj in folder_files:
                    relative_name = obj.key[len(base_prefix):]
                    try:
                        file_data = bucket.Object(obj.key).get()["Body"].read()
                        if do_organize:
                            filename = obj.key.split("/")[-1]
                            folder_part = relative_name.rsplit("/", 1)[0] if "/" in relative_name else ""
                            type_folder = _get_type_folder(filename)
                            source_label = "data_collection" if is_dc else "samples"
                            zip_path = f"{source_label}/{type_folder}/{folder_part}/{filename}" if folder_part else f"{source_label}/{type_folder}/{filename}"
                        else:
                            source_label = "data_collection/" if is_dc else ""
                            zip_path = f"{source_label}{relative_name}"
                        zf.writestr(zip_path, file_data)
                        done += 1
                        pct = round(done / max(total, 1) * 100)
                        yield f"data: {json.dumps({'type':'progress','done':done,'total':total,'pct':pct,'cls':'ok','msg':f'[OK] {relative_name}'})}\n\n"
                    except Exception as exc:
                        failed += 1
                        yield f"data: {json.dumps({'type':'log','cls':'err','msg':f'[FAIL] {relative_name}: {str(exc)}'})}\n\n"

        zip_buffer.seek(0)
        dl_token = str(uuid.uuid4())
        zip_store[dl_token] = {
            "buffer": zip_buffer,
            "filename": f"{cfg['id']}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        }
        logger.info(
            "Download packaging complete: token=%s done=%s failed=%s filename=%s",
            dl_token,
            done,
            failed,
            zip_store[dl_token]["filename"],
        )

        yield f"data: {json.dumps({'type':'ready','token':dl_token,'done':done,'failed':failed,'total':total})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download/get")
async def api_download_get(
    token: str = Query(..., description="Download token"),
    cfg: dict = Depends(get_current_user),
):
    """Serve the pre-packaged ZIP by token. Removes token after serving."""
    logger.info("API request: GET /api/download/get token=%s", token)
    entry = zip_store.pop(token.strip(), None)
    if not entry:
        logger.warning("Download get failed: invalid or expired token=%s", token)
        raise HTTPException(status_code=404, detail="Invalid or expired download token.")

    entry["buffer"].seek(0)
    logger.info("Download get success: serving filename=%s", entry["filename"])
    return StreamingResponse(
        entry["buffer"],
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{entry["filename"]}"',
        },
    )


# ── Entry point

if __name__ == "__main__":
    import uvicorn
    print("=" * 55)
    print("  AgNext S3 Download Manager v2")
    print("  http://localhost:8080")
    print("=" * 55)
    uvicorn.run(app, host="127.0.0.1", port=8080)
