# DocVault

A self-hosted document management web app with OCR, tagging, and full-text search. Built with Flask and Bootstrap, packaged as a single Docker container with optional Tailscale integration for secure remote access.

## Features

- **Document upload** — Drag & drop or click to upload PDFs, PNGs, JPGs, TIFFs, BMPs, and WebP files (up to 50MB)
- **Automatic OCR** — Extracts text from uploaded documents using Tesseract OCR, with background processing and progress tracking
- **Full-text search** — Search across document titles, OCR text, and notes
- **Tagging** — Organize documents with tags; filter by tag from the sidebar
- **Thumbnails** — Auto-generated previews for all document types
- **Dark mode** — Toggle between light and dark themes
- **API docs** — Built-in Swagger UI at `/apidocs`
- **Tailscale auth** — Optional Tailscale Serve integration for HTTPS and identity-based access control
- **Role-based access** — Admin and user roles managed through the maintenance page
- **Mobile-friendly** — Responsive UI with camera capture support on mobile devices

## Quick Start

### Prerequisites

- Docker and Docker Compose

### Run

```bash
git clone https://github.com/matthewjthomas/doc-vault.git
cd doc-vault
docker compose up -d
```

The app will be available at **http://localhost:5050**.

### Development Mode

By default, auth is bypassed when `AUTH_BYPASS=true` is set in `docker-compose.yml`. This gives full admin access without Tailscale. For production, comment out or remove the `AUTH_BYPASS` line.

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `changeme` | Flask secret key for session security |
| `MAX_UPLOAD_MB` | `50` | Maximum upload file size in megabytes |
| `AUTH_BYPASS` | *(unset)* | Set to `true` to bypass Tailscale auth (dev mode) |

### Volumes

| Host Path | Container Path | Purpose |
|---|---|---|
| `./data` | `/app/data` | Database and Tailscale state |
| `./uploads` | `/app/uploads` | Uploaded document files and thumbnails |

The `data/` directory contains:
- `database/docvault.db` — SQLite database
- `tailscale/` — Tailscale daemon state (persistent across restarts)

## Tailscale Integration

DocVault can use [Tailscale Serve](https://tailscale.com/kb/1312/serve) to provide HTTPS access with identity-based authentication. Tailscale Serve injects trusted identity headers (`Tailscale-User-Login`, `Tailscale-User-Name`) into requests, which DocVault uses to identify and authorize users.

### Setup

1. Remove or comment out `AUTH_BYPASS=true` in `docker-compose.yml`
2. Rebuild and start: `docker compose up -d --build`
3. Access via `http://localhost:5050` (temporarily set `AUTH_BYPASS=true` to get in)
4. Go to **Maintenance → Tailscale** tab
5. Enter a hostname (e.g., `docvault`) and click **Enable Tailscale**
6. Follow the Tailscale login link to authenticate
7. Once connected, go to **Maintenance → Users** tab and add allowed Tailscale users with their email and role
8. Remove `AUTH_BYPASS` from `docker-compose.yml` and restart

The app is now accessible at `https://<hostname>.<your-tailnet>.ts.net` with Tailscale handling HTTPS certificates automatically.

### Docker Requirements for Tailscale

The `docker-compose.yml` includes the necessary capabilities:

```yaml
devices:
  - /dev/net/tun:/dev/net/tun
cap_add:
  - NET_ADMIN
  - NET_RAW
```

## Authentication Model

| Access Method | Behavior |
|---|---|
| `AUTH_BYPASS=true` | Full admin access, no auth checks |
| Tailscale Serve (HTTPS via tailnet) | User identified by `Tailscale-User-Login` header, checked against allowed users list |
| Direct access (no bypass, no Tailscale) | Access denied (403) |

### Roles

- **Admin** — Full access including the Maintenance page (Tailscale config, user management, system info)
- **User** — Full document access (upload, search, edit, delete, OCR)

## API

Interactive API documentation is available at `/apidocs` (Swagger UI).

### Key Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/documents` | List documents (paginated, filterable) |
| `POST` | `/api/documents` | Upload a document |
| `GET` | `/api/documents/:id` | Get document details |
| `PUT` | `/api/documents/:id` | Update document metadata |
| `DELETE` | `/api/documents/:id` | Delete a document |
| `GET` | `/api/documents/:id/file` | Download original file |
| `GET` | `/api/documents/:id/status` | Check OCR processing status |
| `POST` | `/api/documents/:id/reocr` | Re-run OCR |
| `GET` | `/api/tags` | List all tags |
| `GET` | `/api/auth/me` | Current user info |
| `GET` | `/api/admin/users` | List allowed users (admin) |
| `GET` | `/api/admin/tailscale/status` | Tailscale status (admin) |
| `GET` | `/api/admin/system` | System info (admin) |

## Tech Stack

- **Backend** — Python 3.11, Flask, pytesseract, pdf2image, Flasgger
- **Frontend** — Bootstrap 5, vanilla JavaScript
- **Database** — SQLite (WAL mode)
- **OCR** — Tesseract OCR
- **Container** — Docker (python:3.11-slim base), Tailscale embedded
