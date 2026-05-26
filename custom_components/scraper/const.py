"""Constants for the Scraper Home Assistant integration."""

DOMAIN = "scraper"

# Legacy sidebar-panel URL path. The panel itself is no longer registered
# (the add-on's ingress panel is canonical); kept only so setup can remove a
# stale panel left by older versions.
PANEL_URL = "scraper"

# Config-entry data keys (required, set during initial config flow)
CONF_STORAGE_URL = "storage_url"
CONF_STORE_ID = "store_id"

# Options-flow keys (optional, can be changed after setup)
CONF_UPLOAD_IMAGES = "upload_images"
CONF_USE_GRAPHQL = "use_graphql"

# Defaults
DEFAULT_UPLOAD_IMAGES = True
DEFAULT_USE_GRAPHQL = True
