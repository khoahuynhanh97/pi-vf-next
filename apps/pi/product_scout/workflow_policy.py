"""Temporary workflow policy: acquire supplied products, pause catalog discovery."""

PRODUCT_DISCOVERY_PAUSED = True
PAUSED_MESSAGE = (
    "Product discovery is paused. Supply an Excel/CSV product list with source links "
    "to python -m tools.spaiauto start --file <path>."
)


def require_product_discovery() -> None:
    if PRODUCT_DISCOVERY_PAUSED:
        raise RuntimeError(PAUSED_MESSAGE)
