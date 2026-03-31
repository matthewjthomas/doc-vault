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
            # Reconnect in background so Flask starts immediately.
            # If already authed, tailscale up completes quickly.
            # If auth is needed, it will block (harmlessly in background)
            # and the user can authenticate from the Maintenance page.
            (
                echo "Reconnecting Tailscale as ${TS_HOSTNAME}..."
                tailscale up --hostname="$TS_HOSTNAME" &
                TS_UP_PID=$!

                # Wait up to 15s for it to finish (already-authed case)
                WAITED=0
                while [ $WAITED -lt 15 ] && kill -0 $TS_UP_PID 2>/dev/null; do
                    sleep 1
                    WAITED=$((WAITED + 1))
                done

                TS_STATE=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")
                if [ "$TS_STATE" = "Running" ]; then
                    echo "Starting Tailscale Serve..."
                    tailscale serve reset 2>/dev/null || true
                    tailscale serve --bg 5000 2>/dev/null || true
                    echo "Tailscale Serve active"
                else
                    echo "Tailscale state: ${TS_STATE}. Waiting for auth or will retry from Maintenance page."
                fi
            ) &
        fi
    fi
fi

# Start the Flask application
exec python app.py
