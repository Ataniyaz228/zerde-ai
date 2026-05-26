"""
Legal Scorer Utility
WebTier классификация по домену + LegalRank inference.
"""

from __future__ import annotations

import re
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
    # Encyclopedias and non-KZ legal sites: useful for context, NOT authoritative
    "wikipedia.org", "ru.wikipedia.org", "en.wikipedia.org",
    "kk.wikipedia.org",
    "wikiwand.com",
    "pravo163.ru",   # Russian law advisory, not KZ jurisdiction
    "consultant.ru", # Russian legal DB, not KZ jurisdiction
    "garant.ru",     # Russian legal DB, not KZ jurisdiction
])

_BLACKLIST_PATTERNS = frozenset([
    "forum", "otvet.mail.ru", "reddit.com", "anon", "pikabu.ru",
    "answers.yahoo.com", "mail.ru/community",
])

# Regex для многофакторной скоринговой модели (v9.4)
_STRICT_CODE_RE = re.compile(
    r"(?i)\b(?:Гражданск\w*|Налогов\w*|Уголовн\w*|Трудов\w*|Экологическ\w*|Бюджетн\w*|Предпринимательск\w*|Административн\w*)\s+кодекс"
)
_KZ_CODE_RE = re.compile(r"(?i)Кодекс\s+Республики\s+Казахстан")
_ARTICLE_REF_RE = re.compile(r"(?i)\b(?:Статья\s+\d+|ст\.\s*\d+|бап\s+\d+)")
_OFFICIAL_MARKER_RE = re.compile(r"(?i)\b(?:закон\s+РК|Республики\s+Казахстан|о\s+внесении\s+изменений)\b")
_FALSE_POSITIVE_RE = re.compile(r"(?i)\b(?:этическ\w*|поведени\w*|корпоративн\w*)\s+кодекс")


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

    # Default: TIER_3 для неизвестных (conservative)
    # Unknown domains shouldn't be elevated to TIER_2 (expert) status
    return WebTier.TIER_3


def infer_legal_rank_from_web_content(
    tier: WebTier,
    title: str,
    content: str,
    url: str,
) -> tuple[LegalRank, float, str]:
    """
    Выводит LegalRank и уровень уверенности на основе содержимого веб-страницы.
    Использует многофакторную скоринговую модель для точного распознавания Кодексов и Законов РК
    с защитой от ложных срабатываний (этические кодексы, блоги, обсуждения).
    """
    if tier == WebTier.BLACKLIST:
        return LegalRank.MEDIA_UNKNOWN, 1.0, "Blacklisted domain/URL."

    # 0. Deterministic Adilet URL prefix matching
    parsed = urlparse(url)
    domain = (parsed.hostname or "").removeprefix("www.")
    path_lower = parsed.path.lower()
    if "adilet.zan.kz" in domain:
        doc_match = re.search(r"/docs/([kzvpu])\d", path_lower)
        if doc_match:
            prefix = doc_match.group(1)
            if prefix == "k":
                return LegalRank.CODE, 1.0, "Adilet URL pattern K-prefix: Code."
            elif prefix == "z":
                return LegalRank.LAW_RK, 1.0, "Adilet URL pattern Z-prefix: Law."
            elif prefix in ("p", "u"):
                return LegalRank.GOVERNMENT_RESOLUTION, 1.0, "Adilet URL pattern P/U-prefix: Government Resolution."
            elif prefix == "v":
                return LegalRank.MINISTERIAL_ORDER, 1.0, "Adilet URL pattern V-prefix: Ministerial Order."

    score = 0
    signals = []

    # 1. Base Domain Signal
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    domain = hostname.removeprefix("www.")
    
    # Check if exact or subdomain matching TIER_1
    is_tier_1 = _domain_matches(domain, _TIER_1_DOMAINS)
    if is_tier_1 or tier == WebTier.TIER_1:
        score += 3
        signals.append("TIER_1 / gov.kz domain (+3)")

    # 2. Strict Code Patterns
    text_to_scan = f"{title} \n {content}"
    
    # Check false positives first
    has_false_positive = bool(_FALSE_POSITIVE_RE.search(text_to_scan))
    
    has_strict_code = bool(_STRICT_CODE_RE.search(text_to_scan) or _KZ_CODE_RE.search(text_to_scan))
    if has_strict_code and not has_false_positive:
        score += 4
        signals.append("Strict Code pattern match (+4)")
    elif "кодекс" in text_to_scan.lower() and not has_false_positive:
        score += 1
        signals.append("Generic 'кодекс' keyword (+1)")

    # 3. Structured Article References
    if _ARTICLE_REF_RE.search(text_to_scan):
        score += 2
        signals.append("Structured article reference detected (+2)")

    # 4. Official Markers
    if _OFFICIAL_MARKER_RE.search(text_to_scan):
        score += 1
        signals.append("Official RK legal terminology (+1)")

    # Evaluate Thresholds
    signals_str = ", ".join(signals) if signals else "No signals"
    if score >= 6:
        inferred = LegalRank.CODE
        confidence = min(1.0, 0.5 + 0.05 * score)
        reason = f"Highly likely a Kazakhstan Code (Score: {score}). Signals: [{signals_str}]"
    elif score >= 4:
        inferred = LegalRank.LAW_RK
        confidence = min(0.9, 0.4 + 0.05 * score)
        reason = f"Likely a Kazakhstan Law (Score: {score}). Signals: [{signals_str}]"
    else:
        inferred = infer_legal_rank_from_tier(tier)
        confidence = 0.5
        reason = f"Fallback to WebTier {tier} mapping (Score: {score}). Signals: [{signals_str}]"

    return inferred, confidence, reason


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
