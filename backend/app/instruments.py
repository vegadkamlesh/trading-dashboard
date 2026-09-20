"""Underlying index registry + option-chain instrument resolution.

Security IDs are taken from Dhan's instrument master. Only the indices you
actually trade need to be listed here. Add more by copying the pattern.

`sec_id`  -> Dhan "UnderlyingScrip" for the index (IDX_I segment).
`name`    -> human label shown in the UI tab.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class IndexInstrument:
    key: str          # short key used in URLs, e.g. "NIFTY"
    name: str         # display name, e.g. "NIFTY 50"
    sec_id: int       # Dhan UnderlyingScrip (IDX_I)
    underlying_seg: str = "IDX_I"
    lot_size: int = 1  # informational; real lot size resolved per contract


# Dhan index security IDs (IDX_I segment).
# These are stable Dhan identifiers for the index value itself.
INDEX_REGISTRY: Dict[str, IndexInstrument] = {
    "NIFTY": IndexInstrument("NIFTY", "NIFTY 50", 13, lot_size=25),
    "SENSEX": IndexInstrument("SENSEX", "SENSEX", 51, lot_size=10),
    # The following are included for convenience; only NIFTY & SENSEX are
    # polled by default (see OC_POLLED_INDICES).
    "BANKNIFTY": IndexInstrument("BANKNIFTY", "BANK NIFTY", 25, lot_size=15),
    "FINNIFTY": IndexInstrument("FINNIFTY", "FIN NIFTY", 27, lot_size=25),
    "MIDCPNIFTY": IndexInstrument("MIDCPNIFTY", "MIDCAP NIFTY", 442, lot_size=50),
}


def get_index(key: str) -> Optional[IndexInstrument]:
    return INDEX_REGISTRY.get(key.upper())


def list_indices() -> List[Dict[str, object]]:
    return [
        {"key": i.key, "name": i.name, "lotSize": i.lot_size}
        for i in INDEX_REGISTRY.values()
    ]
