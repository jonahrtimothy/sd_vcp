"""
Maps screener.in's sector classification strings to the closest matching
Kite NIFTY sector index tradingsymbol, for the "sector relative strength"
half of Section 5's fundamentals filter.

Deliberately incomplete: only sectors with a clear, defensible match to an
actual Kite INDICES-segment instrument are included. An unmapped sector
means "relative strength not computed" (confluence.py must report this
honestly), never a guessed/loose mapping -- matches the project's
no-fabrication discipline (strategy prompt Section 12: "Never fabricate...
state explicitly when data isn't available").

Confirmed against Kite's real NSE INDICES list (Aug 2026) -- every value
here is a tradingsymbol that actually exists, not assumed.
"""

SECTOR_TO_NIFTY_INDEX = {
    "Information Technology": "NIFTY IT",
    "Banks": "NIFTY BANK",
    "Oil, Gas & Consumable Fuels": "NIFTY OIL AND GAS",
    "Automobiles": "NIFTY AUTO",
    "Auto Components": "NIFTY AUTO",
    "Pharmaceuticals & Biotechnology": "NIFTY PHARMA",
    "Healthcare": "NIFTY HEALTHCARE",
    "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Metals & Mining": "NIFTY METAL",
    "Realty": "NIFTY REALTY",
    "Financial Services": "NIFTY FIN SERVICE",
    "Media, Entertainment & Publication": "NIFTY MEDIA",
    "Consumer Durables": "NIFTY CONSR DURBL",
    "Chemicals & Petrochemicals": "NIFTY CHEMICALS",
    "Telecommunication": "NIFTY SERV SECTOR",
}


def get_sector_index(sector: str | None) -> str | None:
    """Returns the Kite tradingsymbol for `sector`, or None if unmapped."""
    if not sector:
        return None
    return SECTOR_TO_NIFTY_INDEX.get(sector.strip())
