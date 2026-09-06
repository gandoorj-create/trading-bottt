"""
kelly.py
Kelly criterion-оор бооцооны хэмжээ тооцох (1/4 Kelly, capped).
"""


def kelly_fraction(true_prob, decimal_odds):
    """Full Kelly fraction: f* = (b*p - q) / b, сөрөг бол 0."""
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - true_prob
    f = (b * true_prob - q) / b
    return max(f, 0.0)


def stake_amount(bankroll, true_prob, decimal_odds, kelly_multiplier=0.25, max_pct=0.03):
    """Bankroll-ийн хэдэн хувийг, ямар мөнгөн дүнгээр тавихыг буцаана.

    Буцаах: (pct, amount). pct нь max_pct-аар хязгаарлагдана (жишээ нь 3%).
    Edge сөрөг бол (0.0, 0.0).
    """
    f = kelly_fraction(true_prob, decimal_odds)
    pct = min(f * kelly_multiplier, max_pct)
    if pct <= 0:
        return 0.0, 0.0
    return pct, round(bankroll * pct, 2)
