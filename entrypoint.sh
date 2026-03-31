#!/bin/sh
set -e

# Create necessary directories
mkdir -p /app/data/database /app/data/tailscale /app/uploads/thumbnails
mkdir -p /var/run/tailscale

STATE_FILE="/app/data/tailscale/tailscaled.state"

# Start tailscaled if state exists (i.e., Tailscale was previously enabled)
if [ -f "$STATE_FILE" ]; then
    echo "Starting tailscaled..."
    tailscaled --state="$STATE_FILE" --socket=/var/run/tailscale/tailscaled.sock &
    sleep 2

    # Check if Tailscale was enabled in settings — read from SQLite
    DB_PATH="/app/data/database/docvault.db"
    if [ -f "$DB_PATH" ]; then
        TS_ENABLED=$(sqlite3 "$DB_PATH" "SELECT value FROM settings WHERE key='tailscale_enabled';" 2>/dev/null || echo "")
        TS_HOSTNAME=$(sqlite3 "$DB_PATH" "SELECT value FROM settings WHERE key='tailscale_hostname';" 2>/dev/null || echo "")

        if [ "$TS_ENABLED" = "true" ] && [ -n "$TS_HOSTNAME" ]; then
            # Check current state before trying to reconnect
            TS_STATE=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")

            if [ "$TS_STATE" = "Running" ]; then
                # Already connected — just start serve
                echo "Tailscale already connected."
                tailscale serve reset 2>/dev/null || true
                tailscale serve --bg 5000 2>/dev/null || true
                echo "Tailscale Serve active"
            elif [ "$TS_STATE" = "NeedsLogin" ]; then
                # Needs auth — don't run tailscale up (it would regen keys).
                # User will authenticate from the Maintenance page.
                echo "Tailscale needs authentication. Use Maintenance page to log in."
            else
                # Try to reconnect in background (already authed, just needs 'up')
                (
                    echo "Reconnecting Tailscale as ${TS_HOSTNAME}..."
                    timeout 10 tailscale up --hostname="$TS_HOSTNAME" 2>/dev/null || true

                    TS_STATE=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")
                    if [ "$TS_STATE" = "Running" ]; then
                        echo "Starting Tailscale Serve..."
                        tailscale serve reset 2>/dev/null || true
                        tailscale serve --bg 5000 2>/dev/null || true
                        echo "Tailscale Serve active"
                    else
                        echo "Tailscale not connected (state: ${TS_STATE}). Use Maintenance page."
                    fi
                ) &
            fi
        fi
    fi
fi

# Start the Flask application
exec python app.py
