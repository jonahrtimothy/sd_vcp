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
here is a tradingsymbol that actually exists, not assumed. Sector KEYS
were originally guessed from screener.in's GICS-like taxonomy, then
corrected against real output from a full 40-symbol watchlist scan (Aug
31 2026) -- e.g. screener.in actually uses the combined string "Automobile
and Auto Components", not separate "Automobiles"/"Auto Components" as
first guessed; that real run is what caught it.

Deliberately left UNMAPPED (confirmed no matching Kite sector index
exists, not just unresearched): "Power" (NTPC, POWERGRID), "Construction"
(LT), "Construction Materials" / cement (ULTRACEMCO, GRASIM), "Services"
(too broad/ambiguous -- ADANIPORTS).
"""

SECTOR_TO_NIFTY_INDEX = {
    "Information Technology": "NIFTY IT",
    "Banks": "NIFTY BANK",
    "Oil, Gas & Consumable Fuels": "NIFTY OIL AND GAS",
    "Automobile and Auto Components": "NIFTY AUTO",
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
