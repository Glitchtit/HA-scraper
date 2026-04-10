"""Constants for the Grocy Scraper Home Assistant integration."""

DOMAIN = "grocy_scraper"

# Sidebar panel
PANEL_TITLE = "Grocy Scraper"
PANEL_ICON = "mdi:barcode-scan"
PANEL_URL = "grocy-scraper"

# Config-entry data keys (required, set during initial config flow)
CONF_STORAGE_URL = "storage_url"
CONF_STORE_ID = "store_id"

# Options-flow keys (optional, can be changed after setup)
CONF_UPLOAD_IMAGES = "upload_images"
CONF_USE_GRAPHQL = "use_graphql"
CONF_GEMINI_API_KEY = "gemini_api_key"
CONF_GEMINI_MODEL = "gemini_model"
CONF_GEMINI_MODEL_OPTIMIZE = "gemini_model_optimize"

# Defaults
DEFAULT_UPLOAD_IMAGES = True
DEFAULT_USE_GRAPHQL = True
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash"
