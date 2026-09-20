#!/bin/sh
set -e

CONFIG_FILE="/config/bot-api/local-bot-api.env"
TRIGGER_FILE="/config/bot-api/restart-trigger"
DATA_DIR="/var/lib/telegram-bot-api"
TEMP_DIR="/tmp/telegram-bot-api"
PORT=8081

CHILD_PID=""
LAST_TRIGGER=""

stop_child() {
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        echo "[supervisor] Sending SIGTERM to child telegram-bot-api (PID $CHILD_PID)..."
        kill -TERM "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
        echo "[supervisor] Child process exited."
        CHILD_PID=""
    fi
}

handle_term() {
    echo "[supervisor] SIGTERM received. Shutting down child process..."
    stop_child
    exit 0
}

handle_int() {
    echo "[supervisor] SIGINT received. Shutting down child process..."
    stop_child
    exit 0
}

trap handle_term TERM
trap handle_int INT

mkdir -p "$DATA_DIR" "$TEMP_DIR" 2>/dev/null || true

while true; do
    API_ID=""
    API_HASH=""

    if [ -f "$CONFIG_FILE" ]; then
        API_ID=$(grep '^API_ID=' "$CONFIG_FILE" 2>/dev/null | cut -d '=' -f2- | tr -d ' \r\n')
        API_HASH=$(grep '^API_HASH=' "$CONFIG_FILE" 2>/dev/null | cut -d '=' -f2- | tr -d ' \r\n')
    fi

    if [ -n "$API_ID" ] && [ -n "$API_HASH" ]; then
        echo "[supervisor] Starting telegram-bot-api child with API_ID=$API_ID..."
        telegram-bot-api \
            --local \
            --api-id="$API_ID" \
            --api-hash="$API_HASH" \
            --http-port="$PORT" \
            --dir="$DATA_DIR" \
            --temp-dir="$TEMP_DIR" &
        CHILD_PID=$!
        echo "[supervisor] telegram-bot-api started with PID $CHILD_PID"
    else
        echo "[supervisor] Credentials not configured in $CONFIG_FILE. Entering standby mode..."
        CHILD_PID=""
    fi

    if [ -f "$TRIGGER_FILE" ]; then
        LAST_TRIGGER=$(stat -c %Y "$TRIGGER_FILE" 2>/dev/null || stat -f %m "$TRIGGER_FILE" 2>/dev/null || date +%s)
    fi

    # Monitoring loop
    while true; do
        # Check if child exited
        if [ -n "$CHILD_PID" ]; then
            if ! kill -0 "$CHILD_PID" 2>/dev/null; then
                echo "[supervisor] Child telegram-bot-api exited. Waiting before restart..."
                CHILD_PID=""
                sleep 2
                break
            fi
        fi

        # Check for reload trigger
        if [ -f "$TRIGGER_FILE" ]; then
            CURRENT_TRIGGER=$(stat -c %Y "$TRIGGER_FILE" 2>/dev/null || stat -f %m "$TRIGGER_FILE" 2>/dev/null || echo "")
            if [ -n "$CURRENT_TRIGGER" ] && [ "$CURRENT_TRIGGER" != "$LAST_TRIGGER" ]; then
                echo "[supervisor] Restart trigger detected! Initiating graceful reload..."
                LAST_TRIGGER="$CURRENT_TRIGGER"
                stop_child
                sleep 1
                break
            fi
        fi

        sleep 1
    done
done
