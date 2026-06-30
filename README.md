# AgNext S3 Download Manager

A web-based tool to browse and download files from AgNext's AWS S3 storage. Authenticates via Keycloak SSO, lists sample and data collection folders by date range, and packages selected files into a ZIP archive with real-time progress.

---

## Prerequisites

- **Python 3.10+** (or Docker)
- **pip** packages: `fastapi`, `uvicorn`, `boto3`, `requests`, `PyJWT`, `cryptography`
- Keycloak SSO access (CentralIAM realm at dev.perfeqtfoods.com)

---

## Quick Start (Local)

### 1. Clone the repo

```bash
git clone <repo-url>
cd AWS_download_tool
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the server

```bash
uvicorn app:app --host 127.0.0.1 --port 8080
```

Or:

```bash
python app.py
```

Server starts at **http://localhost:8080**

Interactive API docs at **http://localhost:8080/docs**

---

## Quick Start (Docker)

### 1. Build & run with docker-compose

```bash
docker-compose build
docker-compose up -d
```

### 2. Or build manually

```bash
docker build -t agnext-s3-download .
docker run -d --name agnext-s3 \
  -p 8080:8080 \
  -v ./config.INI:/app/config.INI \
  agnext-s3-download
```

App available at **http://localhost:8080**

---

## Usage (Step-by-Step)

### Step 1: Login

1. Open `http://localhost:8080` in your browser
2. Keycloak SSO login page appears automatically
3. Enter your credentials and authenticate
4. On success, the app determines your S3 client folder automatically

> Your session persists via JWT token. Click **Logout** in the top-right to end it.

### Step 2: Browse Files

1. Select a date range using:
   - **Quick Presets**: Today, Yesterday, Last 7 days, Last 30 days, This month, FY 2025-26
   - **Manual pickers**: Start Date and End Date
2. Click the **search icon** button
3. Results appear in two tabs:
   - **Samples** — epoch-timestamped sample folders within date range
   - **Data Collection** — commodity/subfolder tree (expandable)
4. Use **multi-search** (comma-separated keywords) to filter results with OR logic
5. Use **Select All** or check individual folders

### Step 3: Download

1. Click **Continue** after selecting folders
2. (Optional) Filter by **file type** checkboxes (images, csv, json, etc.)
3. (Optional) Enable **Organize by Type** to group files in the ZIP by extension
4. Click **Start Download**
5. The app packages selected files into a ZIP with **real-time SSE progress**:
   - Progress bar fills as each file is fetched from S3
   - Per-file log entries show in the console
   - Stats (Downloaded / Failed / Total) update live
6. Once packaging completes, the ZIP auto-downloads to your browser

---

## Configuration

All S3 and API settings live in `config.INI`:

| Section | Key | Description |
|---------|-----|-------------|
| `CONFIG_SETTINGS` | `run_env` | API environment: `Prod`, `QA`, or `Dev` |
| `S3` | `bucket` | S3 bucket name (`agnext-cognito`) |
| `S3` | `bucket_folder` | Base folder prefix (`visio_desktop/`) |
| `S3` | `region` | AWS region (`us-east-2`) |
| `S3` | `pool_id` | Cognito Identity Pool ID |
| `S3` | `type` | Device type (`iot`) |
| `S3` | `client` | Auto-populated on login from qualix.ai API |

> **Note**: The `client` field is overwritten automatically after each login with the correct folder path from the API. You do not need to set it manually.

---

## Project Structure

```
AWS_download_tool/
├── app.py               # FastAPI backend (API routes, S3 logic, SSE progress)
├── web_api_handle.py    # Keycloak JWT validation + S3 client discovery
├── index.html           # Single-page UI
├── silent-check-sso.html # Keycloak silent SSO check
├── config.INI           # S3 + API + Keycloak configuration
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container build (uvicorn)
├── docker-compose.yml   # One-command deployment
└── docs/                # Copilot helper docs and archived notes
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/kc-config` | Return Keycloak public config for frontend |
| `POST` | `/api/token-exchange` | Exchange Keycloak auth code for tokens |
| `GET` | `/api/me` | Validate JWT and return user info |
| `POST` | `/api/logout` | Clear server-side user cache |
| `GET` | `/api/files?start_date=&end_date=&filter=` | List S3 folders by date range |
| `GET` | `/api/dc-expand?prefix=...` | Expand a data_collection subfolder |
| `GET` | `/api/download/prepare?keys=...&organize_by_type=&file_types=` | SSE stream: package files into ZIP |
| `GET` | `/api/download/get?token=...` | Serve the pre-packaged ZIP file |

All endpoints (except `/api/kc-config` and `/api/token-exchange`) require `Authorization: Bearer <JWT>` header.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Unauthorized. Please log in." after search | JWT expired. Click Logout, then log in again |
| Keycloak login page doesn't load | Check `KEYCLOAK` section in config.INI — server URL must be reachable |
| Files take long to load | Large date range with many folders. Narrow the date range |
| Docker build fails | Ensure Docker Desktop is running |
| `uvicorn` not found | Run `pip install uvicorn[standard]` |

---

## Security Notes

- Authentication via Keycloak SSO — credentials never touch this server
- JWT tokens validated against Keycloak public keys (RS256)
- S3 access uses temporary Cognito credentials (auto-expire)
- User config cached in-memory by Keycloak `sub` claim (cleared on logout or restart)
- All S3 key access is prefix-validated to prevent path traversal
