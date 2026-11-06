"""MCX gold and silver futures the Top-Bottom strategy can be assigned.

Angel One's scrip master gives a lot size in the contract's own unit, but MCX quotes a price in another unit (gold per 10 g, silver per kg), so the lot size alone
does not say how many rupees one rupee of price movement is worth. `point_value` does: rupees gained or lost per lot for a price move of 1.00.
The figures follow the MCX contract specifications (checked against the exchange's own launch notices and broker contract sheets in November 2026;
confirm with the exchange before relying on them for a real order). They are used only to show paper P&L in rupees.

  GOLD       1 kg lot,   quoted per 10 g   -> 1 kg / 10 g  = 100 per rupee
  GOLDM      100 g lot,  quoted per 10 g   -> 10
  GOLDTEN    10 g lot,   quoted per 10 g   -> 1
  GOLDGUINEA 8 g lot,    quoted per 8 g    -> 1
  GOLDPETAL  1 g lot,    quoted per 1 g    -> 1
  SILVER     30 kg lot,  quoted per kg     -> 30
  SILVERM    5 kg lot,   quoted per kg     -> 5
  SILVERMIC  1 kg lot,   quoted per kg     -> 1
  SILVER100  100 g lot,  quoted per 10 g   -> 10
"""
from __future__ import annotations

COMMODITIES: dict[str, dict] = {
    "GOLD": {"label": "Gold (1 kg lot)", "metal": "Gold", "point_value": 100},
    "GOLDM": {"label": "Gold Mini (100 g lot)", "metal": "Gold", "point_value": 10},
    "GOLDTEN": {"label": "Gold Ten (10 g lot)", "metal": "Gold", "point_value": 1},
    "GOLDGUINEA": {"label": "Gold Guinea (8 g lot)", "metal": "Gold", "point_value": 1},
    "GOLDPETAL": {"label": "Gold Petal (1 g lot)", "metal": "Gold", "point_value": 1},
    "SILVER": {"label": "Silver (30 kg lot)", "metal": "Silver", "point_value": 30},
    "SILVERM": {"label": "Silver Mini (5 kg lot)", "metal": "Silver", "point_value": 5},
    "SILVERMIC": {"label": "Silver Micro (1 kg lot)", "metal": "Silver", "point_value": 1},
    "SILVER100": {"label": "Silver 100 (100 g lot)", "metal": "Silver", "point_value": 10},
}


def is_commodity(symbol: str) -> bool:
    return symbol in COMMODITIES
