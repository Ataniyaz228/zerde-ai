"""
ЗЕРДЕ v6.2 — Legal Scorer Utility
WebTier классификация по домену + LegalRank inference.
"""

from __future__ import annotations

from urllib.parse import urlparse

from zerde.models import LegalRank, WebTier

# ---------------------------------------------------------------------------
# Domain → WebTier маппинг (§1.2)
# ---------------------------------------------------------------------------

_TIER_1_DOMAINS = frozenset([
    "gov.kz", "adilet.zan.kz", "supreme.kz", "primeminister.kz",
    "president.kz", "parliament.kz", "minjust.gov.kz", "kkm.gov.kz",
    "minfin.gov.kz", "mfa.gov.kz", "stat.gov.kz",
])

_TIER_2_DOMAINS = frozenset([
    "zakon.kz", "tengrinews.kz", "forbes.kz", "profit.kz", "vlast.kz",
    "kapital.kz", "kursiv.kz", "inform.kz", "kazpravda.kz",
    "legalacts.kz",
])

_TIER_3_DOMAINS = frozenset([
    "linkedin.com", "vc.ru", "medium.com", "habr.com", "dtf.ru",
    "tjournal.ru",
])

_BLACKLIST_PATTERNS = frozenset([
    "forum", "otvet.mail.ru", "reddit.com", "anon", "pikabu.ru",
    "answers.yahoo.com", "mail.ru/community",
])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_web_tier(url: str) -> WebTier:
    """
    Определяет WebTier источника по домену URL.
    Приоритет: BLACKLIST → TIER_1 → TIER_2 → TIER_3 → TIER_2 (default).

    Args:
        url: URL источника.

    Returns:
        WebTier значение.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
    except Exception:
        return WebTier.TIER_3

    # Проверка BLACKLIST (паттерны)
    for pattern in _BLACKLIST_PATTERNS:
        if pattern in hostname or pattern in url.lower():
            return WebTier.BLACKLIST

    # Нормализация: убираем www.
    domain = hostname.removeprefix("www.")

    # Проверка по точному домену и поддоменам
    for tier_domains, tier in [
        (_TIER_1_DOMAINS, WebTier.TIER_1),
        (_TIER_2_DOMAINS, WebTier.TIER_2),
        (_TIER_3_DOMAINS, WebTier.TIER_3),
    ]:
        if _domain_matches(domain, tier_domains):
            return tier

    # Default: TIER_2 для неизвестных (conservative)
    return WebTier.TIER_2


def infer_legal_rank_from_tier(tier: WebTier) -> LegalRank:
    """
    Выводит LegalRank из WebTier для Web-источников.

    Rules:
        TIER_1 (Gov) → MINISTERIAL_ORDER (7) — минимальный ранг для гос. источника
        TIER_2 (Expert) → EXPERT_ANALYTICS (10)
        TIER_3 (Blogs) → MEDIA_UNKNOWN (11)
        BLACKLIST → MEDIA_UNKNOWN (11) — не должен доходить сюда
    """
    mapping = {
        WebTier.TIER_1: LegalRank.MINISTERIAL_ORDER,  # 7 — gov web (не НПА)
        WebTier.TIER_2: LegalRank.EXPERT_ANALYTICS,   # 10
        WebTier.TIER_3: LegalRank.MEDIA_UNKNOWN,      # 11
        WebTier.BLACKLIST: LegalRank.MEDIA_UNKNOWN,   # 11
    }
    return mapping[tier]


def get_rank_label(rank: LegalRank) -> str:
    """Возвращает человекочитаемое название ранга на русском."""
    labels = {
        LegalRank.INTERNATIONAL_TREATY: "Международный договор",
        LegalRank.CODE: "Кодекс РК",
        LegalRank.CONSTITUTIONAL_LAW: "Конституционный закон",
        LegalRank.LAW_RK: "Закон РК",
        LegalRank.PRESIDENTIAL_DECREE: "Указ Президента",
        LegalRank.GOVERNMENT_RESOLUTION: "Постановление Правительства",
        LegalRank.MINISTERIAL_ORDER: "Приказ министерства",
        LegalRank.SC_PROSECUTORS_CLARIFICATION: "Разъяснение ВС / Генпрокуратуры",
        LegalRank.SC_CASE_LAW: "Судебная практика ВС",
        LegalRank.EXPERT_ANALYTICS: "Экспертная аналитика",
        LegalRank.MEDIA_UNKNOWN: "СМИ / Неизвестный",
    }
    return labels.get(rank, f"Ранг {int(rank)}")


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _domain_matches(domain: str, tier_domains: frozenset[str]) -> bool:
    """Проверяет точное совпадение домена или принадлежность к поддомену."""
    if domain in tier_domains:
        return True
    # Проверка поддоменов: zan.kz матчит adilet.zan.kz
    for td in tier_domains:
        if domain.endswith(f".{td}") or domain == td:
            return True
    return False
