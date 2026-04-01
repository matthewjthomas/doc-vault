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
- **Trash bin** — Deleted documents are kept for 30 days before permanent removal; admins can restore or permanently delete from the Maintenance → Trash tab
- **Mobile-friendly** — Responsive UI with camera capture support on mobile devices

## Quick Start

### Prerequisites

- Docker and Docker Compose

### Run from source

```bash
git clone https://github.com/matthewjthomas/doc-vault.git
cd doc-vault
docker compose up -d
```

### Run from GHCR

Pull the pre-built image:

```bash
docker pull ghcr.io/matthewjthomas/doc-vault:latest
```

Or use it directly in a `docker-compose.yml`:

```yaml
services:
  docvault:
    image: ghcr.io/matthewjthomas/doc-vault:latest
    container_name: docvault
    ports:
      - "5050:5000"
    volumes:
      - ./data:/app/data
      - ./uploads:/app/uploads
    environment:
      - SECRET_KEY=change-me-to-something-secret
      - MAX_UPLOAD_MB=50
      # - AUTH_BYPASS=true
    restart: unless-stopped
```

Then run:

```bash
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

Tailscale runs in **userspace networking mode** (`--tun=userspace-networking`), so no special Docker capabilities (`NET_ADMIN`, `NET_RAW`) or `/dev/net/tun` device access are needed. All tailnet traffic is proxied through Tailscale Serve rather than a kernel TUN device.

## Share Watch

DocVault can watch a network share or local folder for new files and automatically import them for review. This is useful for connecting a scanner that saves files to a shared folder. Configure from **Maintenance → Share Watch**.

The watcher polls the source at a configurable interval, imports any new files, runs OCR, and flags them as **pending review**. Imported files appear under the **Pending** nav item where you can approve or delete them.

### SMB Share

Set **Source Type** to **SMB Network Share** and enter the SMB path:

```
//server/share/scans
```

Provide the username and password for the share if required.

### Local Folder (NFS, CIFS, SSHFS, etc.)

Any network filesystem can be used by mounting it on the Docker host and bind-mounting it into the container. Set **Source Type** to **Local Folder** and point it at the container path. No username or password is needed.

1. **Mount the NFS share on the host:**

   ```bash
   sudo mkdir -p /mnt/scanner
   sudo mount -t nfs nas.local:/volume1/scans /mnt/scanner
   ```

   To persist across reboots, add to `/etc/fstab`:

   ```
   nas.local:/volume1/scans /mnt/scanner nfs defaults 0 0
   ```

2. **Add the mount to `docker-compose.yml`:**

   ```yaml
   services:
     docvault:
       image: ghcr.io/matthewjthomas/doc-vault:latest
       volumes:
         - ./data:/app/data
         - ./uploads:/app/uploads
         - /mnt/scanner:/app/scanner   # NFS share mounted on host
       environment:
         - SECRET_KEY=change-me-to-something-secret
   ```

3. **Configure Share Watch:** Go to **Maintenance → Share Watch**, set Source Type to **Local Folder**, and enter `/app/scanner` as the folder path. Enable the watcher and save.

The watcher will poll the folder, import new files, and delete them from the source after import — the same as the SMB mode. This works with any mount type: NFS, CIFS, SSHFS, or any other FUSE filesystem.

## Authentication Model

| Access Method | Behavior |
|---|---|
| `AUTH_BYPASS=true` | Full admin access, no auth checks |
| Tailscale Serve (HTTPS via tailnet) | User identified by `Tailscale-User-Login` header, checked against allowed users list |
| Direct access (no bypass, no Tailscale) | Access denied (403) |

> **Note:** Because Tailscale runs in userspace networking mode, direct access via Tailscale IP (`100.x.x.x:5000`) is not available. All tailnet access goes through Tailscale Serve (`https://hostname.ts.net`).

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
