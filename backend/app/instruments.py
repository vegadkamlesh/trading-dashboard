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
    # Where the OPTIONS on this index trade. Needed to place algo orders.
    derivative_segment: str = "NSE_FNO"
    strike_step: int = 50  # fallback strike interval if the chain can't tell us


# Dhan index security IDs (IDX_I segment).
# These are stable Dhan identifiers for the index value itself.
#
# Lot sizes are set by the exchange and DO change. These were read straight out
# of Dhan's scrip master (SEM_LOT_UNITS on OPTIDX rows):
#   https://images.dhan.co/api-data/api-scrip-master.csv
# Verified: NIFTY 65, SENSEX 20, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120.
# If the exchange revises one, update this table or set ALGO_LOT_SIZES
# (e.g. "NIFTY=65,SENSEX=20") - the override always wins.
# Lot sizes are set by the exchange and can change. Keep them current so the
# order ticket's lot→units math is correct (quantity is entered in LOTS).
INDEX_REGISTRY: Dict[str, IndexInstrument] = {
    "NIFTY": IndexInstrument("NIFTY", "NIFTY 50", 13, lot_size=65,
                             derivative_segment="NSE_FNO", strike_step=50),
    "SENSEX": IndexInstrument("SENSEX", "SENSEX", 51, lot_size=20,
                              derivative_segment="BSE_FNO", strike_step=100),
    # The following are included for convenience; only NIFTY & SENSEX are
    # polled by default (see OC_POLLED_INDICES).
    "BANKNIFTY": IndexInstrument("BANKNIFTY", "BANK NIFTY", 25, lot_size=30,
                                 derivative_segment="NSE_FNO", strike_step=100),
    "FINNIFTY": IndexInstrument("FINNIFTY", "FIN NIFTY", 27, lot_size=60,
                                derivative_segment="NSE_FNO", strike_step=50),
    "MIDCPNIFTY": IndexInstrument("MIDCPNIFTY", "MIDCAP NIFTY", 442, lot_size=120,
                                  derivative_segment="NSE_FNO", strike_step=25),
}


def get_index(key: str) -> Optional[IndexInstrument]:
    return INDEX_REGISTRY.get(key.upper())


def lot_size_for(key: str) -> int:
    """Lot size for an index, honouring the ALGO_LOT_SIZES override."""
    from .config import get_settings

    override = get_settings().algo_lot_override
    if key.upper() in override:
        return override[key.upper()]
    inst = get_index(key)
    return inst.lot_size if inst else 1


def list_indices() -> List[Dict[str, object]]:
    return [
        {
            "key": i.key,
            "name": i.name,
            "lotSize": lot_size_for(i.key),
            "derivativeSegment": i.derivative_segment,
        }
        for i in INDEX_REGISTRY.values()
    ]
