import asyncio
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

from store import store, utc_now


MAX_BATCH_SIZE = 12
DEFAULT_REVIEW_CONCURRENCY = 4
DEFAULT_REVIEW_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_TOKENS = 350
RETRYABLE_CACHED_STATUSES = {"unconfigured", "error"}
RETRYABLE_RISK_FLAGS = {
    "ai_not_configured",
    "ai_insufficient_quota",
    "ai_rate_limited",
    "ai_provider_unavailable",
    "openai_insufficient_quota",
    "openai_rate_limited",
    "ai_review_failed",
}
HEURISTIC_MODEL = "local-pair-heuristic-v1"
VALID_VERDICTS = {"analog", "inverse", "related_not_analog", "different", "uncertain"}
VALID_YES_MAPPINGS = {"kalshi_yes", "kalshi_no", "unknown"}
DEFAULT_LOCAL_BASE_URLS = ("http://localhost:11434/v1", "http://localhost:1234/v1")
LOCAL_MODEL_HINTS = ("qwen", "llama", "mistral", "gemma", "phi")
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


@dataclass(frozen=True)
class AiReviewClient:
    provider: str
    model: str
    base_url: str
    api_style: str
    api_key: str = ""

COUNTRY_ALIASES: list[tuple[str, str]] = [
    (r"\busa\b", "usa"),
    (r"\bunited states\b", "usa"),
    (r"\bargentina\b", "argentina"),
    (r"\bfrance\b", "france"),
    (r"\bspain\b", "spain"),
    (r"\bengland\b", "england"),
    (r"\bbrazil\b", "brazil"),
    (r"\bgermany\b", "germany"),
    (r"\bportugal\b", "portugal"),
    (r"\bnetherlands\b", "netherlands"),
    (r"\bbelgium\b", "belgium"),
    (r"\bitaly\b", "italy"),
    (r"\bmexico\b", "mexico"),
    (r"\bcanada\b", "canada"),
    (r"\bmorocco\b", "morocco"),
    (r"\bcroatia\b", "croatia"),
    (r"\becuador\b", "ecuador"),
    (r"\begypt\b", "egypt"),
    (r"\bcongo\b", "congo"),
    (r"\bparaguay\b", "paraguay"),
    (r"\bcape verde\b", "cape_verde"),
    (r"\bcolombia\b", "colombia"),
    (r"\bivory coast\b", "ivory_coast"),
    (r"\bsenegal\b", "senegal"),
    (r"\buruguay\b", "uruguay"),
    (r"\bjapan\b", "japan"),
    (r"\bsouth korea\b", "south_korea"),
    (r"\bnorth korea\b", "north_korea"),
    (r"\baustralia\b", "australia"),
    (r"\bnorway\b", "norway"),
    (r"\balgeria\b", "algeria"),
    (r"\bswitzerland\b", "switzerland"),
    (r"\baustria\b", "austria"),
    (r"\bsouth africa\b", "south_africa"),
    (r"\bghana\b", "ghana"),
    (r"\btunisia\b", "tunisia"),
    (r"\bdenmark\b", "denmark"),
    (r"\bsweden\b", "sweden"),
    (r"\bpoland\b", "poland"),
    (r"\bchile\b", "chile"),
    (r"\bnigeria\b", "nigeria"),
    (r"\bturkey\b", "turkey"),
    (r"\bscotland\b", "scotland"),
    (r"\bnew zealand\b", "new_zealand"),
    (r"\bsaudi arabia\b", "saudi_arabia"),
    (r"\biran\b", "iran"),
    (r"\buzbekistan\b", "uzbekistan"),
    (r"\bcosta rica\b", "costa_rica"),
    (r"\bpanama\b", "panama"),
    (r"\bqatar\b", "qatar"),
]


PAIR_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["analog", "inverse", "related_not_analog", "different", "uncertain"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "yes_mapping": {
            "type": "string",
            "enum": ["kalshi_yes", "kalshi_no", "unknown"],
        },
        "preferred": {"type": "boolean"},
        "summary": {"type": "string"},
        "settlement_analysis": {"type": "string"},
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
    },
    "required": [
        "verdict",
        "confidence",
        "yes_mapping",
        "preferred",
        "summary",
        "settlement_analysis",
        "risk_flags",
    ],
}


def pair_review_status() -> dict[str, Any]:
    clients = _configured_ai_clients()
    return {
        "configured": bool(clients),
        "localFirst": _local_first_enabled(),
        "concurrency": _review_concurrency(),
        "timeoutSeconds": _review_timeout_seconds(),
        "maxOutputTokens": _max_output_tokens(),
        "providers": [
            {
                "provider": client.provider,
                "model": client.model,
                "apiStyle": client.api_style,
                "baseUrl": _safe_base_url(client.base_url),
                "hasApiKey": bool(client.api_key),
            }
            for client in clients
        ],
        "maxBatchSize": MAX_BATCH_SIZE,
        "heuristicModel": HEURISTIC_MODEL,
    }


def _configured_ai_clients() -> list[AiReviewClient]:
    explicit_provider = os.getenv("PAIR_REVIEW_PROVIDER", "").strip().lower()
    if explicit_provider in {"disabled", "none", "off", "local"}:
        return []

    if explicit_provider:
        client = _client_from_provider(explicit_provider, explicit=True)
        return [client] if client else []

    clients: list[AiReviewClient] = []
    local_clients = _auto_local_clients()
    if _local_first_enabled():
        clients.extend(local_clients)

    generic_key = os.getenv("PAIR_REVIEW_API_KEY", "").strip()
    generic_base = os.getenv("PAIR_REVIEW_BASE_URL", "").strip()
    generic_model = os.getenv("PAIR_REVIEW_MODEL", "").strip()
    if generic_base or generic_key or generic_model:
        client = _client_from_provider("openai_compatible", explicit=True)
        if client:
            clients.append(client)

    gemini_client = _client_from_provider("gemini", explicit=False)
    if gemini_client:
        clients.append(gemini_client)

    openai_client = _client_from_provider("openai", explicit=False)
    if openai_client:
        clients.append(openai_client)

    openrouter_client = _client_from_provider("openrouter", explicit=False)
    if openrouter_client and openrouter_client not in clients:
        clients.append(openrouter_client)

    if not _local_first_enabled():
        clients.extend(local_clients)

    return _dedupe_clients(clients)


def _local_first_enabled() -> bool:
    value = os.getenv("PAIR_REVIEW_LOCAL_FIRST", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _auto_local_clients() -> list[AiReviewClient]:
    enabled = os.getenv("PAIR_REVIEW_FREE_LOCAL", "auto").strip().lower()
    if enabled in {"0", "false", "no", "off", "disabled"}:
        return []

    configured_model = (
        os.getenv("PAIR_REVIEW_LOCAL_MODEL", "").strip()
        or os.getenv("OLLAMA_PAIR_REVIEW_MODEL", "").strip()
    )
    local_key = os.getenv("PAIR_REVIEW_LOCAL_API_KEY", "").strip() or os.getenv("OLLAMA_API_KEY", "").strip()
    base_urls = _local_base_urls()
    clients: list[AiReviewClient] = []

    for base_url in base_urls:
        models = _discover_local_models(base_url)
        model = configured_model or _pick_local_model(models)
        if not model:
            continue
        clients.append(
            AiReviewClient(
                provider=_local_provider_name(base_url),
                model=model,
                base_url=base_url,
                api_style="chat",
                api_key=local_key,
            )
        )

    return clients


def _local_base_urls() -> list[str]:
    raw = os.getenv("PAIR_REVIEW_LOCAL_BASE_URLS", "").strip()
    if not raw:
        raw = os.getenv("OLLAMA_BASE_URL", "").strip()
    if not raw:
        return list(DEFAULT_LOCAL_BASE_URLS)
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def _discover_local_models(base_url: str) -> list[str]:
    models = _discover_openai_compatible_models(base_url)
    if models:
        return models
    return _discover_ollama_native_models(base_url)


def _discover_openai_compatible_models(base_url: str) -> list[str]:
    try:
        with httpx.Client(timeout=0.35) as client:
            resp = client.get(_api_endpoint(base_url, "models"))
            if resp.status_code >= 400:
                return []
            data = resp.json()
    except Exception:
        return []

    raw_models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return []
    ids: list[str] = []
    for item in raw_models:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, str):
            ids.append(item)
    return ids


def _discover_ollama_native_models(base_url: str) -> list[str]:
    native_base = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    try:
        with httpx.Client(timeout=0.35) as client:
            resp = client.get(f"{native_base}/api/tags")
            if resp.status_code >= 400:
                return []
            data = resp.json()
    except Exception:
        return []

    raw_models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return []
    ids: list[str] = []
    for item in raw_models:
        if isinstance(item, dict) and item.get("name"):
            ids.append(str(item["name"]))
    return ids


def _pick_local_model(models: list[str]) -> str:
    if not models:
        return ""
    lower = [(model.lower(), model) for model in models]
    for hint in LOCAL_MODEL_HINTS:
        for lowered, original in lower:
            if hint in lowered:
                return original
    return models[0]


def _local_provider_name(base_url: str) -> str:
    lowered = base_url.lower()
    if "11434" in lowered:
        return "ollama"
    if "1234" in lowered:
        return "lmstudio"
    return "local"


def _dedupe_clients(clients: list[AiReviewClient]) -> list[AiReviewClient]:
    seen: set[str] = set()
    deduped: list[AiReviewClient] = []
    for client in clients:
        key = _client_key(client)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(client)
    return deduped


def _client_from_provider(provider: str, *, explicit: bool) -> AiReviewClient | None:
    provider = provider.replace("-", "_")
    api_style = os.getenv("PAIR_REVIEW_API_STYLE", "").strip().lower()
    generic_key = os.getenv("PAIR_REVIEW_API_KEY", "").strip()
    generic_model = os.getenv("PAIR_REVIEW_MODEL", "").strip()
    generic_base = os.getenv("PAIR_REVIEW_BASE_URL", "").strip()

    if provider == "openai":
        api_key = generic_key or os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return AiReviewClient(
            provider="openai",
            model=generic_model or os.getenv("OPENAI_PAIR_REVIEW_MODEL", os.getenv("OPENAI_MODEL", "gpt-5-mini")),
            base_url=generic_base or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_style=api_style or "responses",
            api_key=api_key,
        )

    if provider in {"openai_compatible", "compatible", "custom"}:
        api_key = generic_key
        base_url = generic_base
        model = generic_model
        if not base_url or not model:
            return None
        return AiReviewClient(
            provider="openai_compatible",
            model=model,
            base_url=base_url,
            api_style=api_style or "chat",
            api_key=api_key,
        )

    if provider == "gemini":
        api_key = generic_key or os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            return None
        return AiReviewClient(
            provider="gemini",
            model=generic_model or os.getenv("GEMINI_PAIR_REVIEW_MODEL", DEFAULT_GEMINI_MODEL),
            base_url=generic_base or os.getenv("GEMINI_BASE_URL", DEFAULT_GEMINI_BASE_URL),
            api_style=api_style or "chat",
            api_key=api_key,
        )

    if provider == "openrouter":
        api_key = generic_key or os.getenv("OPENROUTER_API_KEY", "").strip()
        model = generic_model or os.getenv("OPENROUTER_PAIR_REVIEW_MODEL", "").strip()
        if not api_key or not model:
            return None
        return AiReviewClient(
            provider="openrouter",
            model=model,
            base_url=generic_base or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            api_style=api_style or "chat",
            api_key=api_key,
        )

    if provider == "groq":
        api_key = generic_key or os.getenv("GROQ_API_KEY", "").strip()
        model = generic_model or os.getenv("GROQ_PAIR_REVIEW_MODEL", "").strip()
        if not api_key or not model:
            return None
        return AiReviewClient(
            provider="groq",
            model=model,
            base_url=generic_base or os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            api_style=api_style or "chat",
            api_key=api_key,
        )

    if provider == "deepseek":
        api_key = generic_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
        model = generic_model or os.getenv("DEEPSEEK_PAIR_REVIEW_MODEL", "").strip()
        if not api_key or not model:
            return None
        return AiReviewClient(
            provider="deepseek",
            model=model,
            base_url=generic_base or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            api_style=api_style or "chat",
            api_key=api_key,
        )

    if provider in {"ollama", "lmstudio", "lm_studio"}:
        default_base = "http://localhost:1234/v1" if provider in {"lmstudio", "lm_studio"} else "http://localhost:11434/v1"
        base_url = generic_base or os.getenv("OLLAMA_BASE_URL", default_base)
        model = (
            generic_model
            or os.getenv("PAIR_REVIEW_LOCAL_MODEL", "").strip()
            or os.getenv("OLLAMA_PAIR_REVIEW_MODEL", "").strip()
            or _pick_local_model(_discover_local_models(base_url))
        )
        enabled = explicit or os.getenv("PAIR_REVIEW_OLLAMA_ENABLED", "").strip().lower() in {"1", "true", "yes"}
        if not model or not enabled:
            return None
        return AiReviewClient(
            provider=_local_provider_name(base_url),
            model=model,
            base_url=base_url,
            api_style=api_style or "chat",
            api_key=generic_key or os.getenv("OLLAMA_API_KEY", "").strip(),
        )

    return None


def _safe_base_url(value: str) -> str:
    return re.sub(r"//([^/@:]+):([^/@]+)@", "//***:***@", value.rstrip("/"))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _review_concurrency() -> int:
    return _env_int("PAIR_REVIEW_CONCURRENCY", DEFAULT_REVIEW_CONCURRENCY, minimum=1, maximum=12)


def _review_timeout_seconds() -> float:
    return _env_float(
        "PAIR_REVIEW_TIMEOUT_SECONDS",
        DEFAULT_REVIEW_TIMEOUT_SECONDS,
        minimum=5.0,
        maximum=120.0,
    )


def _max_output_tokens() -> int:
    return _env_int("PAIR_REVIEW_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, minimum=120, maximum=1200)


async def review_pair_batch(payload: dict[str, Any]) -> dict[str, Any]:
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        raise ValueError("Expected body: { pairs: [...] }")

    force = bool(payload.get("force"))
    requested = pairs[:MAX_BATCH_SIZE]
    clients = _configured_ai_clients()
    disabled_clients: set[str] = set()
    disabled_reasons: dict[str, list[str]] = {}
    semaphore = asyncio.Semaphore(_review_concurrency())

    async def process_item(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        async with semaphore:
            return await _review_pair_item(
                index,
                item,
                force=force,
                clients=clients,
                disabled_clients=disabled_clients,
                disabled_reasons=disabled_reasons,
            )

    results = await asyncio.gather(*(process_item(index, item) for index, item in enumerate(requested)))
    ordered = sorted(results, key=lambda result: result[0])
    reviews = [review for _, review, _ in ordered]
    errors = [error for _, _, item_errors in ordered for error in item_errors]

    return {
        "configured": bool(clients),
        "provider": clients[0].provider if clients else "local",
        "model": clients[0].model if clients else HEURISTIC_MODEL,
        "providers": [
            {"provider": client.provider, "model": client.model, "apiStyle": client.api_style}
            for client in clients
        ],
        "reviews": reviews,
        "errors": errors,
        "maxBatchSize": MAX_BATCH_SIZE,
        "concurrency": _review_concurrency(),
    }


async def _review_pair_item(
    index: int,
    item: dict[str, Any],
    *,
    force: bool,
    clients: list[AiReviewClient],
    disabled_clients: set[str],
    disabled_reasons: dict[str, list[str]],
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    normalized = _normalize_candidate(item)
    pair_key = normalized["pairKey"]
    rules_hash = _rules_hash(normalized)

    cached = None if force else store.get_ai_pair_review(pair_key, rules_hash)
    if cached and clients and _cached_review_should_retry(cached):
        cached = None
    if cached:
        return index, cached, errors

    active_clients = [client for client in clients if _client_key(client) not in disabled_clients]
    if not active_clients:
        review = _heuristic_review(
            normalized,
            fallback_reason=_merged_disabled_reasons(disabled_reasons) or ["ai_not_configured"],
            error=None,
        )
        return index, review, errors

    last_error: dict[str, Any] | None = None
    last_exception: Exception | None = None
    for client in active_clients:
        try:
            ai_review = await _call_ai_review(client, normalized)
            review = _base_review(
                normalized,
                status="reviewed",
                verdict=ai_review["verdict"],
                confidence=float(ai_review["confidence"]),
                yes_mapping=ai_review["yes_mapping"],
                preferred=bool(ai_review["preferred"]),
                summary=ai_review["summary"],
                settlement_analysis=ai_review["settlement_analysis"],
                risk_flags=list(ai_review.get("risk_flags") or []),
                model=f"{client.provider}:{client.model}",
            )
            return (
                index,
                store.upsert_ai_pair_review(
                    pair_key,
                    normalized["poly"]["id"],
                    normalized["kalshi"]["id"],
                    rules_hash,
                    "reviewed",
                    review,
                ),
                errors,
            )
        except Exception as exc:
            error_info = _friendly_ai_error(exc, client.provider)
            last_error = error_info
            last_exception = exc
            errors.append(
                {
                    "pairKey": pair_key,
                    "provider": client.provider,
                    "model": client.model,
                    "message": error_info["summary"],
                }
            )
            if error_info.get("disable_provider"):
                key = _client_key(client)
                disabled_clients.add(key)
                disabled_reasons[key] = list(error_info["risk_flags"])
            continue
    review = _heuristic_review(
        normalized,
        fallback_reason=list((last_error or {}).get("risk_flags") or ["ai_review_failed"]),
        error=str(last_exception) if last_exception else None,
    )
    return index, review, errors


def _cached_review_should_retry(review: dict[str, Any]) -> bool:
    status = str(review.get("status") or "")
    if status not in RETRYABLE_CACHED_STATUSES:
        return False
    flags = {str(flag) for flag in review.get("riskFlags") or []}
    if flags & RETRYABLE_RISK_FLAGS:
        return True
    try:
        confidence = float(review.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    return status == "error" and confidence <= 0


def _client_key(client: AiReviewClient) -> str:
    return f"{client.provider}:{client.model}:{client.base_url}:{client.api_style}"


def _merged_disabled_reasons(disabled_reasons: dict[str, list[str]]) -> list[str]:
    reasons: list[str] = []
    for flags in disabled_reasons.values():
        for flag in flags:
            if flag not in reasons:
                reasons.append(flag)
    return reasons


def _heuristic_review(
    candidate: dict[str, Any],
    *,
    fallback_reason: str | list[str],
    error: str | None,
) -> dict[str, Any]:
    poly_features = _market_features(candidate["poly"])
    kalshi_features = _market_features(candidate["kalshi"])
    shared_entities = sorted(poly_features["entities"] & kalshi_features["entities"])
    shared_competitions = sorted(poly_features["competitions"] & kalshi_features["competitions"])
    shared_years = sorted(poly_features["years"] & kalshi_features["years"])
    same_gender = (
        not poly_features["gender"]
        or not kalshi_features["gender"]
        or poly_features["gender"] == kalshi_features["gender"]
    )
    kind_score = _heuristic_kind_score(poly_features["kind"], kalshi_features["kind"])
    poly_score = str(poly_features.get("score") or "")
    kalshi_score = str(kalshi_features.get("score") or "")
    score_mismatch = bool(poly_score and kalshi_score and poly_score != kalshi_score)
    score_match = bool(poly_score and kalshi_score and poly_score == kalshi_score)
    poly_tie_policy = str(poly_features.get("tie_policy") or "")
    kalshi_tie_policy = str(kalshi_features.get("tie_policy") or "")
    tie_policy_mismatch = bool(poly_tie_policy and kalshi_tie_policy and poly_tie_policy != kalshi_tie_policy)

    fallback_flags = [fallback_reason] if isinstance(fallback_reason, str) else fallback_reason
    risk_flags = ["heuristic_fallback", *[flag for flag in fallback_flags if flag]]
    if score_mismatch:
        verdict = "related_not_analog"
        confidence = 0.94
        yes_mapping = "unknown"
        summary = "Local heuristic: same event family, but different exact scoreline."
        settlement = f"The extracted score signatures differ ({poly_score} vs {kalshi_score}), so these exact-score markets cannot settle together."
    elif tie_policy_mismatch and shared_entities and shared_competitions:
        verdict = "related_not_analog"
        confidence = 0.93
        yes_mapping = "unknown"
        summary = "Local heuristic: same furthest-advancing proposition, but tie settlement differs."
        settlement = (
            f"The markets share entity and competition, but tie policy differs "
            f"({poly_tie_policy} vs {kalshi_tie_policy}). That creates divergent payouts/resolution "
            "when multiple host nations reach the same furthest stage."
        )
    elif score_match:
        verdict = "analog"
        confidence = 0.91
        yes_mapping = "kalshi_yes"
        summary = "Local heuristic: same exact scoreline."
        settlement = f"Both markets extracted the same exact-score signature ({poly_score}), so their YES outcomes appear aligned."
    elif shared_entities and shared_competitions and shared_years and kind_score >= 0.85 and same_gender:
        verdict = "analog"
        confidence = 0.92
        yes_mapping = "kalshi_yes"
        summary = "Local heuristic: same entity, competition, year, and proposition mechanics."
        settlement = (
            "Both markets appear to resolve Yes under the same competition/entity condition in the same year. "
            "This is a high-confidence fallback review because OpenAI was unavailable."
        )
    elif shared_entities and shared_competitions and kind_score < 0.85:
        verdict = "related_not_analog"
        confidence = 0.82
        yes_mapping = "unknown"
        summary = "Local heuristic: same entity and competition, but different proposition type."
        settlement = (
            f"Both markets reference {', '.join(shared_entities)} in {', '.join(shared_competitions)}, "
            f"but the proposition kinds differ ({poly_features['kind']} vs {kalshi_features['kind']})."
        )
    elif shared_competitions and not shared_entities:
        verdict = "related_not_analog"
        confidence = 0.7
        yes_mapping = "unknown"
        summary = "Local heuristic: same competition but different or missing entity."
        settlement = "The markets share a competition, but the named team/entity does not match clearly."
    elif shared_entities and not shared_competitions:
        verdict = "related_not_analog"
        confidence = 0.62
        yes_mapping = "unknown"
        summary = "Local heuristic: same entity but different competition or settlement scope."
        settlement = "The markets share an entity, but the competition/scope does not match clearly."
    else:
        verdict = "different"
        confidence = 0.75
        yes_mapping = "unknown"
        summary = "Local heuristic: no matching entity and competition found."
        settlement = "The fallback review did not find a shared entity plus shared competition."

    return _base_review(
        candidate,
        status="heuristic",
        verdict=verdict,
        confidence=confidence,
        yes_mapping=yes_mapping,
        preferred=verdict in {"analog", "inverse"} and confidence >= 0.8,
        summary=summary,
        settlement_analysis=settlement,
        risk_flags=risk_flags,
        model=HEURISTIC_MODEL,
        error=error,
    )


def _market_features(market: dict[str, Any]) -> dict[str, Any]:
    raw_title = _raw_text(
        " ".join(
            str(market.get(key) or "")
            for key in ("title", "event_title", "category")
        )
    )
    raw_rules = _raw_text(str(market.get("rules") or ""))
    raw_full_text = f"{raw_title} {raw_rules}".strip()
    title_text = _normalize_text(
        " ".join(
            str(market.get(key) or "")
            for key in ("title", "event_title", "category")
        )
    )
    rules_text = _normalize_text(str(market.get("rules") or ""))
    text = f"{title_text} {rules_text}".strip()
    title_kind = _extract_kind(title_text)
    return {
        "entities": set(_extract_entities(text)),
        "competitions": set(_extract_competitions(text)),
        "years": set(re.findall(r"\b20[2-9][0-9]\b", text)),
        "gender": _extract_gender(title_text) or _extract_gender(text),
        "kind": title_kind if title_kind != "generic" else _extract_kind(text),
        "score": _extract_score_signature(raw_title, raw_full_text),
        "tie_policy": _extract_tie_policy(text),
    }


def _raw_text(value: str) -> str:
    text = unicodedata.normalize("NFD", value.lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("u.s.a.", "usa").replace("u.s.", "usa")
    text = re.sub(r"\bunited states(?: of america)?\b", "usa", text)
    text = re.sub(r"\bmen['â€™]?s\b", "mens", text)
    text = re.sub(r"\bwomen['â€™]?s\b", "womens", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFD", value.lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("u.s.a.", "usa").replace("u.s.", "usa")
    text = re.sub(r"\bunited states(?: of america)?\b", "usa", text)
    text = re.sub(r"\bmen['’]?s\b", "mens", text)
    text = re.sub(r"\bwomen['’]?s\b", "womens", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_entities(text: str) -> list[str]:
    entities: list[str] = []
    for pattern, entity in COUNTRY_ALIASES:
        if re.search(pattern, text) and entity not in entities:
            entities.append(entity)
    return entities


def _extract_entities_ordered(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for pattern, entity in COUNTRY_ALIASES:
        match = re.search(pattern, text)
        if match and entity not in [item[1] for item in found]:
            found.append((match.start(), entity))
    return [entity for _, entity in sorted(found)]


def _extract_competitions(text: str) -> list[str]:
    competitions: list[str] = []
    if re.search(r"\bwomens world cup\b", text):
        competitions.append("womens_world_cup")
    elif re.search(r"\b(?:fifa )?(?:mens )?world cup\b", text):
        competitions.append("world_cup")
    if re.search(r"\bus open\b", text):
        competitions.append("us_open")
    if re.search(r"\bfrench open\b|\broland garros\b", text):
        competitions.append("french_open")
    if re.search(r"\bwimbledon\b", text):
        competitions.append("wimbledon")
    return competitions


def _extract_gender(text: str) -> str:
    if re.search(r"\bwomens\b|\bwomen\b|\bfemale\b", text):
        return "women"
    if re.search(r"\bmens\b|\bmen\b|\bmale\b", text):
        return "men"
    if "world cup" in text:
        return "men"
    return ""


def _extract_kind(text: str) -> str:
    if re.search(r"\b(exact score|correct score|final score|score be)\b", text) and re.search(r"\b\d{1,2}\s+\d{1,2}\b|\b\d{1,2}\s*-\s*\d{1,2}\b", text):
        return "exact_score"
    if re.search(r"\bbest performing host\b|\bfurthest advancing host\b|\bhost nation\b", text):
        return "best_host"
    if re.search(r"\bannounced as (?:a )?hosts?\b|\bwho will host\b|\bhost for the\b", text):
        return "host"
    if re.search(r"\bwinner of\b.*\bcome from\b|\bcome from\b.*\bwinner\b", text):
        return "regional_winner"
    if re.search(r"\beliminated\b|\belimination\b|\bknocked out\b|\bfail to advance\b", text):
        return "elimination"
    if re.search(r"\badvance\b|\badvancing\b|\bqualify\b|\bqualifies\b", text):
        return "advance"
    if re.search(r"\bover\b|\bunder\b|\btotal\b", text):
        return "total"
    if re.search(r"\bspread\b|\bhandicap\b", text):
        return "spread"
    if re.search(r"\bwin\b|\bwins\b|\bwinner\b|\bchampion\b", text):
        return "winner"
    if re.search(r"\bworld cup\b", text):
        return "tournament"
    return "generic"


def _extract_tie_policy(text: str) -> str:
    if re.search(r"\b1\s*/\s*n\b|\btied participants\b|\byes payout\b|\brounded down\b", text):
        return "fractional_tie_payout"
    if re.search(r"\bmore total wins\b|\bmore total goals\b|\bconceded fewer\b|\balphabetically\b", text):
        return "single_winner_tiebreak"
    return ""


def _heuristic_kind_score(poly_kind: str, kalshi_kind: str) -> float:
    if poly_kind == kalshi_kind:
        return 1.0
    if {poly_kind, kalshi_kind} == {"winner", "best_host"}:
        return 0.15
    if {poly_kind, kalshi_kind} == {"winner", "host"}:
        return 0.05
    if {poly_kind, kalshi_kind} == {"winner", "regional_winner"}:
        return 0.3
    if {poly_kind, kalshi_kind} <= {"winner", "generic"}:
        return 0.8
    if {poly_kind, kalshi_kind} == {"advance", "elimination"}:
        return 0.45
    return 0.2


def _extract_score_signature(title: str, full_text: str) -> str:
    relevant = title if re.search(r"\b(exact score|correct score|final score|score be)\b", title) else full_text
    if not re.search(r"\b(exact score|correct score|final score|score be)\b", relevant):
        return ""
    score = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b", relevant)
    if not score:
        return ""
    first = int(score.group(1))
    second = int(score.group(2))
    title_entities = _extract_entities_ordered(title)
    text_entities = _extract_entities_ordered(full_text)
    teams = text_entities[:2] if len(text_entities) >= 2 else []
    winner = _extract_score_winner(title, first, second)

    if len(teams) >= 2:
        home, away = teams[0], teams[1]
        home_score, away_score = first, second
        if winner == away:
            home_score, away_score = second, first
        elif winner not in {"", "draw", home} and len(title_entities) >= 2 and title_entities[0] == away:
            home_score, away_score = second, first
        return f"{home}:{home_score}:{away}:{away_score}"

    if len(title_entities) >= 2:
        return f"{title_entities[0]}:{first}:{title_entities[1]}:{second}"

    return f"{winner or 'unknown'}:{first}-{second}"


def _extract_score_winner(title: str, first: int, second: int) -> str:
    if first == second or re.search(r"\bdraw\b", title):
        return "draw"
    match = re.search(r"\b([a-z][a-z ]{1,50}?)\s+wins?\s+\d{1,2}\s*[-–]\s*\d{1,2}\b", title)
    if not match:
        return ""
    entities = _extract_entities_ordered(match.group(1))
    return entities[0] if entities else ""


def _normalize_candidate(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("Each pair must be an object")
    poly = item.get("poly")
    kalshi = item.get("kalshi")
    if not isinstance(poly, dict) or not isinstance(kalshi, dict):
        raise ValueError("Each pair must include poly and kalshi market objects")
    if not poly.get("id") or not kalshi.get("id"):
        raise ValueError("Both markets need ids")
    return {
        "pairKey": item.get("pairKey") or f"{poly['id']}::{kalshi['id']}",
        "poly": _compact_market(poly),
        "kalshi": _compact_market(kalshi),
        "discovery": item.get("discovery") if isinstance(item.get("discovery"), dict) else {},
    }


def _compact_market(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(market.get("id") or ""),
        "source": str(market.get("source") or ""),
        "title": str(market.get("title") or ""),
        "event_title": str(market.get("event_title") or ""),
        "category": str(market.get("category") or ""),
        "outcomes": market.get("outcomes") if isinstance(market.get("outcomes"), list) else [],
        "rules": str(market.get("rules") or ""),
        "end_date": str(market.get("end_date") or ""),
        "url": str(market.get("url") or ""),
        "yes_bid": float(market.get("yes_bid") or 0),
        "yes_ask": float(market.get("yes_ask") or 0),
        "no_bid": float(market.get("no_bid") or 0),
        "no_ask": float(market.get("no_ask") or 0),
    }


def _rules_hash(candidate: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "poly": _hash_market(candidate["poly"]),
            "kalshi": _hash_market(candidate["kalshi"]),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_market(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": market["id"],
        "title": market["title"],
        "event_title": market["event_title"],
        "outcomes": market["outcomes"],
        "rules": market["rules"],
        "end_date": market["end_date"],
    }


async def _call_ai_review(client: AiReviewClient, candidate: dict[str, Any]) -> dict[str, Any]:
    system_prompt, user_payload = _review_prompt(candidate)
    if client.api_style == "responses":
        return await _call_responses_review(client, system_prompt, user_payload)
    return await _call_chat_review(client, system_prompt, user_payload)


def _review_prompt(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    system_prompt = (
        "You review prediction-market candidate pairs for arbitrage discovery. "
        "Decide whether the two markets settle on the same real-world proposition, "
        "including whether one side is the inverse of the other. Read titles, outcomes, "
        "rules, close dates, and settlement criteria carefully. Do not discard merely "
        "because wording, venue terminology, close dates, or UI labels differ. Only mark "
        "related_not_analog or different when settlement scope, event, time period, line, "
        "team/entity, scoreline, or resolution condition materially differs. For exact-score "
        "markets, the score and winner/draw side must match exactly: a 2-1 win, 2-0 win, "
        "and 2-2 draw are all different settlement conditions. Keep possible analogs "
        "as uncertain with a useful confidence instead of over-pruning. Return only JSON "
        "matching the requested schema."
    )
    user_payload = {
        "task": "Analyze whether these two markets are valid analog/inverse counterparts for manual pair discovery.",
        "verdict_guidance": {
            "analog": "Both YES outcomes resolve true under materially the same condition.",
            "inverse": "Polymarket YES resolves true when Kalshi NO resolves true, or vice versa.",
            "related_not_analog": "Same subject/event but materially different proposition, stage, time period, line, or settlement scope.",
            "different": "Not the same subject/event/proposition.",
            "uncertain": "Could be analog/inverse but rules are incomplete, ambiguous, or require human check.",
        },
        "required_json_fields": {
            "verdict": sorted(VALID_VERDICTS),
            "confidence": "number from 0 to 1",
            "yes_mapping": sorted(VALID_YES_MAPPINGS),
            "preferred": "boolean, true only for high-confidence analog/inverse pairs",
            "summary": "short one-sentence judgment",
            "settlement_analysis": "specific comparison of resolution mechanics",
            "risk_flags": "array of short snake_case strings",
        },
        "candidate": candidate,
    }
    return system_prompt, user_payload


async def _call_responses_review(
    client: AiReviewClient,
    system_prompt: str,
    user_payload: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "model": client.model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "max_output_tokens": _max_output_tokens(),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "pair_review",
                "strict": True,
                "schema": PAIR_REVIEW_SCHEMA,
            }
        },
    }
    data = await _post_ai_json(client, _api_endpoint(client.base_url, "responses"), body)
    text = data.get("output_text") or _extract_output_text(data)
    if not text:
        raise RuntimeError(f"{client.provider} response did not include JSON text")
    return _coerce_ai_review(_parse_ai_json(text))


async def _call_chat_review(
    client: AiReviewClient,
    system_prompt: str,
    user_payload: dict[str, Any],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": client.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0,
        "max_tokens": _max_output_tokens(),
    }
    response_format = os.getenv("PAIR_REVIEW_RESPONSE_FORMAT", "json_object").strip().lower()
    if response_format == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "pair_review",
                "strict": True,
                "schema": PAIR_REVIEW_SCHEMA,
            },
        }
    elif response_format != "none":
        body["response_format"] = {"type": "json_object"}

    data = await _post_ai_json(client, _api_endpoint(client.base_url, "chat/completions"), body)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"{client.provider} chat response did not include choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"{client.provider} chat response did not include JSON content")
    return _coerce_ai_review(_parse_ai_json(text))


async def _post_ai_json(client: AiReviewClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if client.api_key:
        headers["Authorization"] = f"Bearer {client.api_key}"
    if client.provider == "openrouter":
        site_url = os.getenv("OPENROUTER_SITE_URL", "http://localhost:3000").strip()
        app_name = os.getenv("OPENROUTER_APP_NAME", "Arbitroly").strip()
        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_name:
            headers["X-Title"] = app_name

    async with httpx.AsyncClient(timeout=_review_timeout_seconds()) as http_client:
        resp = await http_client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(_format_ai_http_error(client, resp.status_code, resp.text))
        return resp.json()


def _api_endpoint(base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    suffix = endpoint.strip("/")
    if base.endswith(f"/{suffix}") or base.endswith(suffix):
        return base
    return f"{base}/{suffix}"


def _parse_ai_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI review JSON must be an object")
    return parsed


def _coerce_ai_review(parsed: dict[str, Any]) -> dict[str, Any]:
    verdict = str(parsed.get("verdict") or "uncertain").strip()
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"AI review returned invalid verdict: {verdict}")

    yes_mapping = str(parsed.get("yes_mapping") or parsed.get("yesMapping") or "unknown").strip()
    if yes_mapping not in VALID_YES_MAPPINGS:
        yes_mapping = "unknown"

    risk_flags = parsed.get("risk_flags") or parsed.get("riskFlags") or []
    if not isinstance(risk_flags, list):
        risk_flags = []

    return {
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence") or 0))),
        "yes_mapping": yes_mapping,
        "preferred": bool(parsed.get("preferred")) and verdict in {"analog", "inverse"},
        "summary": str(parsed.get("summary") or "").strip()[:500],
        "settlement_analysis": str(parsed.get("settlement_analysis") or parsed.get("settlementAnalysis") or "").strip()[:2000],
        "risk_flags": [str(flag).strip()[:80] for flag in risk_flags[:8] if str(flag).strip()],
    }


def _format_ai_http_error(client: AiReviewClient, status_code: int, response_text: str) -> str:
    try:
        data = json.loads(response_text)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "ai_error")
            message = str(err.get("message") or "").strip()
            return f"{client.provider} {status_code} {code}: {message}"[:300]
    except json.JSONDecodeError:
        pass
    return f"{client.provider} HTTP {status_code}: {response_text[:240]}"


def _friendly_ai_error(exc: Exception, provider: str) -> dict[str, Any]:
    raw = str(exc)
    lowered = raw.lower()
    provider_label = _provider_label(provider)
    if "insufficient_quota" in lowered or "quota" in lowered or "billing" in lowered or "credit" in lowered:
        flags = ["ai_insufficient_quota"]
        if provider == "openai":
            flags.append("openai_insufficient_quota")
        return {
            "summary": f"AI review paused: {provider_label} quota or billing is not available.",
            "settlement_analysis": (
                "The provider rejected the review before analysis. This is a quota/billing issue, not a settlement-condition "
                "judgment, so the candidate remains visible as a broad first-pass suggestion."
            ),
            "risk_flags": flags,
            "disable_provider": True,
        }
    if "rate_limit" in lowered or "429" in lowered or "too many requests" in lowered:
        flags = ["ai_rate_limited"]
        if provider == "openai":
            flags.append("openai_rate_limited")
        return {
            "summary": f"AI review paused: {provider_label} rate limit was reached.",
            "settlement_analysis": "The review can be retried later. The candidate remains visible as a broad first-pass suggestion.",
            "risk_flags": flags,
            "disable_provider": True,
        }
    if "connect" in lowered or "timeout" in lowered or "network" in lowered:
        return {
            "summary": f"AI review paused: {provider_label} is unreachable.",
            "settlement_analysis": "The provider did not return a review, so local heuristics were used.",
            "risk_flags": ["ai_provider_unavailable"],
            "disable_provider": True,
        }
    return {
        "summary": f"AI review failed on {provider_label}. Candidate kept as a broad first-pass suggestion.",
        "settlement_analysis": "The AI review did not complete, so this candidate was not discarded.",
        "risk_flags": ["ai_review_failed"],
        "disable_provider": False,
    }


def _provider_label(provider: str) -> str:
    labels = {
        "openai": "OpenAI",
        "openai_compatible": "OpenAI-compatible provider",
        "gemini": "Gemini",
        "openrouter": "OpenRouter",
        "groq": "Groq",
        "deepseek": "DeepSeek",
        "ollama": "Ollama",
        "lmstudio": "LM Studio",
        "local": "local AI provider",
    }
    return labels.get(provider, provider or "AI provider")


def _extract_output_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for output in data.get("output") or []:
        for content in output.get("content") or []:
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _base_review(
    candidate: dict[str, Any],
    *,
    status: str,
    verdict: str,
    confidence: float,
    yes_mapping: str,
    preferred: bool,
    summary: str,
    settlement_analysis: str,
    risk_flags: list[str],
    model: str,
    error: str | None = None,
) -> dict[str, Any]:
    review = {
        "pairKey": candidate["pairKey"],
        "polyId": candidate["poly"]["id"],
        "kalshiId": candidate["kalshi"]["id"],
        "status": status,
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, confidence)),
        "yesMapping": yes_mapping,
        "preferred": preferred,
        "summary": summary.strip(),
        "settlementAnalysis": settlement_analysis.strip(),
        "riskFlags": risk_flags,
        "model": model,
        "reviewedAt": utc_now(),
    }
    if error:
        review["error"] = error
    return review
