"""ISO 18245 Merchant Category Codes — top-100 reference set used for
schema-level validation in DataValidator.

The list intentionally covers the most-frequent retail, fuel, transport,
hospitality, services and financial MCCs. It is deliberately kept as a
``frozenset[str]`` because validator hot-paths perform millions of
membership checks per ETL run.
"""
from __future__ import annotations

# 100 well-known MCC codes (ISO 18245).
_MCC_CODES: tuple[str, ...] = (
    # Auto / fuel
    "5511", "5521", "5531", "5532", "5533", "5541", "5542", "5571",
    "5599",
    # Transport / travel
    "4111", "4112", "4119", "4121", "4131", "4214", "4215", "4225",
    "4411", "4511", "4582", "4722", "4784", "4789",
    # Telecom / utilities
    "4812", "4814", "4816", "4821", "4829", "4899", "4900",
    # Wholesale & specialty retail
    "5013", "5021", "5039", "5044", "5045", "5046", "5047", "5065",
    "5072", "5074", "5085", "5094", "5099", "5111", "5122", "5137",
    "5139", "5169", "5172", "5192", "5193", "5198", "5199",
    # General retail
    "5200", "5211", "5251", "5261", "5300", "5309", "5310", "5311",
    "5331", "5399",
    # Groceries / food
    "5411", "5422", "5441", "5451", "5462", "5499",
    # Apparel & accessories
    "5611", "5621", "5631", "5641", "5651", "5655", "5661", "5681",
    "5691", "5699",
    # Furnishings, electronics
    "5712", "5713", "5714", "5722", "5732", "5733", "5734", "5735",
    # Restaurants & bars
    "5811", "5812", "5813", "5814",
    # Health & beauty
    "5912", "5921", "5931", "5933", "5940", "5941", "5942", "5945",
    "5946", "5947", "5970", "5977", "5992", "5993", "5995", "5999",
    # Lodging
    "7011",
    # Personal services
    "7211", "7216", "7230", "7298", "7299",
    # Recreation
    "7832", "7841", "7995",
    # Health care
    "8011", "8021", "8062", "8099",
    # Education / professional
    "8211", "8398",
    # Misc
    "8999",
)

MCC_REGISTRY: frozenset[str] = frozenset(_MCC_CODES)
"""ISO-18245 reference set used by :class:`app.validator.DataValidator`."""

assert len(MCC_REGISTRY) >= 100, "MCC registry must list ≥100 codes"


def is_valid_mcc(mcc: str | None) -> bool:
    """True iff ``mcc`` is a 4-digit string present in the registry."""
    if not mcc or not isinstance(mcc, str):
        return False
    if len(mcc) != 4 or not mcc.isdigit():
        return False
    return mcc in MCC_REGISTRY
