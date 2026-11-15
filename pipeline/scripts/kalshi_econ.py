"""kalshi_econ.py — Kalshi contract economics + model sizing.

Sportsbook odds -> Kalshi binary price, Kalshi taker fee, Kelly sizing.
Fee formula (Kalshi general fee, standard 7% schedule):
    fee_per_contract = 0.07 * p * (1 - p)          (p in dollars)
    total_fee = ceil(fee_per_contract * contracts * 100) / 100   (dollars)
"""

import math

FEE_MULT = 0.07  # Kalshi standard trading-fee multiplier


def implied_price(american):
    """American odds -> Kalshi YES price in dollars for the picked side."""
    o = float(american)
    if o < 0:
        return (-o) / (100 + (-o))          # -118 -> 118/218 = 0.541
    return 100.0 / (100.0 + o)              # +150 -> 100/250 = 0.400


def fee_per_contract(p):
    return FEE_MULT * p * (1.0 - p)


def order_fee(p, contracts):
    raw = fee_per_contract(p) * contracts
    return math.ceil(raw * 100) / 100  # dollars, rounded up to the cent


def contracts_for_stake(stake, p):
    """How many $1 contracts a given dollar stake buys at price p (taker, fee included)."""
    if p <= 0 or p >= 1:
        return 0
    return max(0, int(stake / p))


def pnl(contracts, p, win):
    """Paper P&L of buying `contracts` YES at price p, incl. fee. Win -> $1/contract."""
    f = order_fee(p, contracts)
    if win:
        return contracts * (1.0 - p) - f
    return -contracts * p - f


def kelly_fraction(true_p, price):
    """Full-Kelly fraction of bankroll for buying YES at `price` with true prob true_p."""
    if price <= 0 or price >= 1:
        return 0.0
    return (true_p - price) / (1.0 - price)
