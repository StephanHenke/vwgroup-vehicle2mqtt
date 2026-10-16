"""Constants for the VW EU Data Act portal API."""

from __future__ import annotations

from . import __version__

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_CLIENT_ID = "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = BASE_URL + "/login"

VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
RELATION_PATH = "/proxy_api/vum/v2/users/me/relations/{vin}"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"

NO_CONTENT_SUFFIX = "_no_content_found.zip"
USER_AGENT = f"vwgroup-vehicle2mqtt/{__version__}"

# Brand-specific portal clients; identifiers are public, not account secrets.
BRAND_CLIENT_IDS = {
    "AUDI": "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com",
    "VOLKSWAGEN": OIDC_CLIENT_ID,
    "VOLKSWAGEN_PASSENGER_CARS": OIDC_CLIENT_ID,
    "VOLKSWAGEN_COMMERCIAL_VEHICLES": OIDC_CLIENT_ID,
    "VW": OIDC_CLIENT_ID,
    "VWN": OIDC_CLIENT_ID,
    "VW_COMMERCIAL": OIDC_CLIENT_ID,
    "SKODA": "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com",
    "SEAT": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
    "CUPRA": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
    "BENTLEY": "d38aac0f-3d89-4a63-8538-b75b31322c7b@apps_vw-dilab_com",
}

BRAND_STATE_ALIASES = {
    "VW": "VOLKSWAGEN_PASSENGER_CARS",
    "VOLKSWAGEN": "VOLKSWAGEN_PASSENGER_CARS",
    "VWN": "VOLKSWAGEN_COMMERCIAL_VEHICLES",
    "VW_COMMERCIAL": "VOLKSWAGEN_COMMERCIAL_VEHICLES",
}
