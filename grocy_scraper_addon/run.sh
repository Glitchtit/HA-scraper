#!/usr/bin/with-contenv bashio

bashio::log.info "Starting Grocy Scraper add-on..."

# ── Required options ──────────────────────────────────────────────────────────
export GROCY_BASE_URL
export GROCY_API_KEY
export KRUOKA_STORE_ID
GROCY_BASE_URL=$(bashio::config 'grocy_url')
GROCY_API_KEY=$(bashio::config 'grocy_api_key')
KRUOKA_STORE_ID=$(bashio::config 'store_id')

if bashio::var.is_empty "${GROCY_BASE_URL}"; then
    bashio::log.fatal "grocy_url is required. Please configure the add-on."
    exit 1
fi
if bashio::var.is_empty "${GROCY_API_KEY}"; then
    bashio::log.fatal "grocy_api_key is required. Please configure the add-on."
    exit 1
fi

# ── Optional Barcode Buddy options ────────────────────────────────────────────
if bashio::config.has_value 'bbuddy_url'; then
    export BARCODEBDY_URL
    BARCODEBDY_URL=$(bashio::config 'bbuddy_url')
fi
if bashio::config.has_value 'bbuddy_api_key'; then
    export BARCODEBDY_API
    BARCODEBDY_API=$(bashio::config 'bbuddy_api_key')
fi
if bashio::config.has_value 'bbuddy_user'; then
    export BARCODEBDY_USER
    BARCODEBDY_USER=$(bashio::config 'bbuddy_user')
fi
if bashio::config.has_value 'bbuddy_password'; then
    export BARCODEBDY_PASSWORD
    BARCODEBDY_PASSWORD=$(bashio::config 'bbuddy_password')
fi

# ── Feature flags ─────────────────────────────────────────────────────────────
IMAGES_FLAG=""
if ! bashio::config.true 'upload_images'; then
    IMAGES_FLAG="--no-images"
fi

GRAPHQL_FLAG=""
if ! bashio::config.true 'use_graphql'; then
    GRAPHQL_FLAG="--no-graphql"
fi

INTERVAL=$(bashio::config 'discover_interval')

# ── Main loop ─────────────────────────────────────────────────────────────────
while true; do
    if bashio::config.has_value 'bbuddy_url' && bashio::config.has_value 'bbuddy_user'; then
        bashio::log.info "Running Grocy Scraper in discover mode..."
        # shellcheck disable=SC2086
        python3 /app/main.py --discover ${IMAGES_FLAG} ${GRAPHQL_FLAG} || \
            bashio::log.warning "Discover run exited with an error; will retry next interval."
    else
        bashio::log.warning "Barcode Buddy URL and/or username not configured; skipping discover run."
    fi

    bashio::log.info "Sleeping ${INTERVAL} minutes until next run..."
    sleep $(( INTERVAL * 60 ))
done
