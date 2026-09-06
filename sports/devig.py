"""
devig.py
Sharp book (Pinnacle)-ийн ханшнаас vig (overround) цэвэрлэж бодит магадлал гаргах.

Пропорционал (multiplicative) арга: implied probability бүрийг нийт
overround-д харьцуулж нормчилно. Хамгийн энгийн бөгөөд өргөн хэрэглэгддэг
арга — favorite/longshot хазайлт засдаггүй ч 2-3 талт h2h/handicap зах
зээлд практикт хангалттай нийцтэй.
"""


def implied_prob(decimal_odds):
    if not decimal_odds or decimal_odds <= 1.0:
        return None
    return 1.0 / decimal_odds


def devig_multiplicative(decimal_odds_list):
    """decimal ханшийн жагсаалтыг vig арилгасан магадлал руу хөрвүүлнэ.

    Буцаах жагсаалтын нийлбэр яг 1.0 болно. Аль нэг ханш хүчингүй бол None.
    """
    implied = [implied_prob(o) for o in decimal_odds_list]
    if any(p is None for p in implied):
        return None
    overround = sum(implied)
    if overround <= 0:
        return None
    return [p / overround for p in implied]
