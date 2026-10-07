from __future__ import annotations

import logging

import httpx

from paper_search.config.settings import ModelProfile, Settings

logger = logging.getLogger(__name__)


async def resolve_model_capabilities(
    settings: Settings, client: httpx.AsyncClient
) -> Settings:
    """Refresh provider capabilities after an explicit operator request."""
    resolved = {}
    for name, profile in settings.models.items():
        resolved[name] = await _resolve_profile(profile, client)
    return Settings.model_validate(
        settings.model_copy(update={"models": resolved}).model_dump()
    )


async def _resolve_profile(
    profile: ModelProfile, client: httpx.AsyncClient
) -> ModelProfile:
    if not profile.capabilities_url or not profile.api_key:
        return profile.model_copy(update={"capability_source": "configured_override"})
    try:
        response = await client.get(
            profile.capabilities_url,
            params={"model": profile.model},
            headers={"Authorization": f"Bearer {profile.api_key}"},
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("output", {}).get("models", ())
        item = next(
            candidate for candidate in models if candidate.get("model") == profile.model
        )
        info = item["model_info"]
        context_window = int(info["context_window"])
        max_output_tokens = int(info["max_output_tokens"])
        if context_window < 8192 or max_output_tokens < 1024:
            raise ValueError("provider returned invalid model capacities")
    except (httpx.HTTPError, KeyError, StopIteration, TypeError, ValueError) as exc:
        logger.warning(
            "Model capability discovery failed for %s; using explicit configured "
            "override: %s",
            profile.model,
            exc,
        )
        return profile.model_copy(update={"capability_source": "configured_override"})
    return profile.model_copy(
        update={
            "context_window": context_window,
            "max_output_tokens_supported": max_output_tokens,
            "features": tuple(
                dict.fromkeys(
                    (*item.get("features", ()), *item.get("capabilities", ()))
                )
            ),
            "capability_source": "provider_catalog",
        }
    )
