# AWS S3 Download Tool — Web App

> Internal tool for browsing and downloading scan samples from S3.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements_web.txt

# 2. Set a secure secret key (required for production)
export FLASK_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# 3. Run the app
python app.py
# App starts at http://localhost:5000
```

---

## Configuration (`config.INI`)

| Section            | Key              | Description                            |
|--------------------|------------------|----------------------------------------|
| `CONFIG_SETTINGS`  | `run_env`        | API environment: `Dev` / `Qa` / `Prod` |
| `API_ENV`          | `prod` / `dev`   | Base URL for qualix.ai API             |
| `S3`               | `bucket`         | S3 bucket name (`agnext-cognito`)      |
| `S3`               | `bucket_folder`  | S3 prefix (`visio_desktop/`)           |
| `S3`               | `client`         | Client folder inside bucket (`FCI`)    |
| `S3`               | `pool_id`        | Cognito Identity Pool ID               |
| `S3`               | `region`         | AWS region (`us-east-2`)               |

---

## Key Entity Relationships

```
qualix.ai OAuth API
  └── POST /portal/login  →  access_token + customer_name
                               │
                               ▼
                     config.INI (COMMODITY.customer updated)

AWS Cognito Identity Pool (unauthenticated)
  └── get_id + get_credentials_for_identity
        └── temp AWS credentials (AccessKeyId / SecretKey / SessionToken)
              └── boto3 S3 resource
                    └── Bucket: agnext-cognito
                          └── Prefix: visio_desktop/FCI/
                                └── Objects grouped by first subfolder
                                      = Sample ID
```

---

## Web App Routes

| Route             | Method    | Description                                  |
|-------------------|-----------|----------------------------------------------|
| `/`               | GET       | Redirect to login or samples                 |
| `/login`          | GET/POST  | Login form — updates config.INI on success   |
| `/logout`         | GET       | Clear session                                |
| `/samples`        | GET       | Sample browser page (HTML)                   |
| `/api/samples`    | GET       | JSON — paginated sample list with date filter|
| `/api/download`   | POST      | Stream ZIP of selected / all samples         |

### `/api/samples` query params
- `page` (int, default 1)
- `page_size` (int: 10/20/50/100, default 20)
- `date` (YYYY-MM-DD, optional)

### `/api/download` POST body (JSON)
```json
{
  "sample_ids": ["SAMPLE_001", "SAMPLE_002"],
  "all": false,
  "date": "2026-05-29"
}
```

---

## Security Notes (OWASP Top 10)

- **A2** — `FLASK_SECRET_KEY` from env var; tokens not persisted client-side
- **A3** — Sample IDs validated against allowlist before S3 key construction
- **A5** — Debug mode disabled by default; credentials never logged
- **A7** — Jinja2 auto-escaping + JS `escHtml()` for dynamic DOM insertion

---

## Files Added

```
app.py                  Flask application (routes, session management)
web_api_handle.py       Login API helper (PyQt5-free)
web_s3_handle.py        S3 listing + ZIP download (PyQt5-free)
requirements_web.txt    Python dependencies
templates/
  base.html             Bootstrap 5 layout + navbar
  login.html            Login form with show/hide password
  samples.html          Sample table, date filter, pagination, download
  error.html            Generic error page
docs/copilot-helpers/
  README.md             This file
```
