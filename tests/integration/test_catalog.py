"""Catalog integration tests: plans, models, availability, and the seed step."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.cli import seed_catalog
from app.core.config import Settings, get_settings
from app.db.models import Model
from app.db.seed import (
    CHAT_MESSAGE_CREDITS,
    IMAGE_CREDITS,
    IMAGE_MODEL_ID,
    MOCK_PROVIDER,
    VIDEO_CREDITS,
    VIDEO_CREDITS_10S,
    VIDEO_MODEL_ID,
    upsert_models,
    upsert_plans,
)
from app.db.session import create_session_factory, session_scope
from app.main import create_app
from tests.support import asgi_client, assert_problem, make_settings

MODEL_FIELDS = {
    "id",
    "name",
    "provider",
    "modality",
    "description",
    "badge",
    "creditCost",
    "creditCost10s",
    "capabilities",
    "contextWindow",
    "sortOrder",
}
PLAN_FIELDS = {
    "id",
    "name",
    "priceCents",
    "monthlyCredits",
    "tagline",
    "features",
    "highlighted",
    "sortOrder",
}
CONFIGURED_CHAT = {
    "ai_base_url": "https://gateway.example.test/v1",
    "ai_api_key": "test-key-not-a-real-secret",
}


async def seed(engine: AsyncEngine, settings: Settings) -> None:
    """Write the catalogs the way the deploy step does."""
    async with session_scope(create_session_factory(engine)) as session:
        await upsert_plans(session)
        await upsert_models(session, settings)


@pytest.fixture
async def catalog_client(
    migrated_database: str, engine: AsyncEngine
) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose catalog is seeded for mock mode with no chat provider."""
    settings = make_settings()
    await seed(engine, settings)
    async with asgi_client(create_app(settings)) as http:
        yield http


# --- Plans -------------------------------------------------------------------


async def test_plans_match_the_seed(catalog_client: httpx.AsyncClient) -> None:
    response = await catalog_client.get("/v1/plans")

    assert response.status_code == 200
    plans = response.json()
    assert [plan["id"] for plan in plans] == ["free", "pro", "studio"]
    assert [(plan["priceCents"], plan["monthlyCredits"]) for plan in plans] == [
        (0, 50),
        (1900, 1000),
        (4900, 3000),
    ]
    for plan in plans:
        assert set(plan) == PLAN_FIELDS
        assert plan["features"]
        assert isinstance(plan["highlighted"], bool)
    assert [plan["sortOrder"] for plan in plans] == [0, 1, 2]


async def test_plans_are_public(catalog_client: httpx.AsyncClient) -> None:
    """No session cookie and an empty cookie jar still return the catalog."""
    assert catalog_client.cookies.get("nero_session") is None

    response = await catalog_client.get("/v1/plans")

    assert response.status_code == 200


# --- Models ------------------------------------------------------------------


async def test_mock_models_are_listed_and_priced_from_the_seed(
    catalog_client: httpx.AsyncClient,
) -> None:
    response = await catalog_client.get("/v1/models")

    assert response.status_code == 200
    models = {model["id"]: model for model in response.json()}
    assert set(models) == {IMAGE_MODEL_ID, VIDEO_MODEL_ID}

    image = models[IMAGE_MODEL_ID]
    assert set(image) == MODEL_FIELDS
    assert image["modality"] == "image"
    assert image["provider"] == MOCK_PROVIDER
    assert image["creditCost"] == IMAGE_CREDITS
    assert image["creditCost10s"] is None
    assert image["contextWindow"] is None
    assert image["capabilities"]["aspectRatios"]

    video = models[VIDEO_MODEL_ID]
    assert video["modality"] == "video"
    assert video["creditCost"] == VIDEO_CREDITS
    assert video["creditCost10s"] == VIDEO_CREDITS_10S
    assert video["badge"] == "Demo"
    assert video["capabilities"]["durations"] == [5, 10]


async def test_models_are_ordered_for_display(catalog_client: httpx.AsyncClient) -> None:
    models = (await catalog_client.get("/v1/models")).json()

    assert [model["sortOrder"] for model in models] == sorted(
        model["sortOrder"] for model in models
    )


@pytest.mark.parametrize("modality", ["chat", "image", "video"])
async def test_modality_filter(catalog_client: httpx.AsyncClient, modality: str) -> None:
    response = await catalog_client.get("/v1/models", params={"modality": modality})

    assert response.status_code == 200
    models = response.json()
    assert all(model["modality"] == modality for model in models)
    if modality == "chat":
        assert models == []
    else:
        assert {model["id"] for model in models} == {
            IMAGE_MODEL_ID if modality == "image" else VIDEO_MODEL_ID
        }


@pytest.mark.parametrize("modality", ["audio", "IMAGE", "", "chat,image"])
async def test_an_unknown_modality_is_rejected(
    catalog_client: httpx.AsyncClient, modality: str
) -> None:
    response = await catalog_client.get("/v1/models", params={"modality": modality})

    assert_problem(response, 400, "invalid_request")
    if modality:
        assert modality not in response.text


async def test_the_unconfigured_chat_model_is_never_advertised(
    catalog_client: httpx.AsyncClient,
) -> None:
    """No provider configuration means no chat model, even though the row exists."""
    listed = {model["id"] for model in (await catalog_client.get("/v1/models")).json()}
    assert "deepseek-v4.1-flash" not in listed

    detail = await catalog_client.get("/v1/models/deepseek-v4.1-flash")

    body = assert_problem(detail, 404, "not_found")
    assert body["detail"] == "That model does not exist."


async def test_an_unknown_model_is_not_found(catalog_client: httpx.AsyncClient) -> None:
    response = await catalog_client.get("/v1/models/no-such-model")

    assert_problem(response, 404, "not_found")


async def test_a_configured_chat_provider_is_advertised(
    migrated_database: str, engine: AsyncEngine
) -> None:
    settings = make_settings(**CONFIGURED_CHAT)
    await seed(engine, settings)

    async with asgi_client(create_app(settings)) as http:
        models = {model["id"]: model for model in (await http.get("/v1/models")).json()}
        chat_detail = await http.get(f"/v1/models/{settings.ai_chat_model}")
        filtered = await http.get("/v1/models", params={"modality": "chat"})

    assert set(models) == {settings.ai_chat_model, IMAGE_MODEL_ID, VIDEO_MODEL_ID}
    chat = models[settings.ai_chat_model]
    assert set(chat) == MODEL_FIELDS
    assert chat["modality"] == "chat"
    assert chat["provider"] == "Coarena"
    assert chat["creditCost"] == CHAT_MESSAGE_CREDITS
    assert chat["contextWindow"] == 128_000
    assert chat["capabilities"]["streaming"] is True
    # The provider-facing slug is internal plumbing and must not leak.
    assert "providerModelId" not in chat

    assert chat_detail.status_code == 200
    assert chat_detail.json() == chat
    assert [model["id"] for model in filtered.json()] == [settings.ai_chat_model]


async def test_live_mode_hides_the_mock_image_and_video_models(
    migrated_database: str, engine: AsyncEngine
) -> None:
    live = make_settings(generation_mode="live", **CONFIGURED_CHAT)
    await seed(engine, live)

    async with asgi_client(create_app(live)) as http:
        models = {model["id"]: model for model in (await http.get("/v1/models")).json()}
        hidden = await http.get(f"/v1/models/{IMAGE_MODEL_ID}")

    assert set(models) == {live.ai_chat_model}
    assert_problem(hidden, 404, "not_found")
    assert IMAGE_MODEL_ID not in hidden.text


async def test_live_mode_without_a_chat_provider_lists_nothing(
    migrated_database: str, engine: AsyncEngine
) -> None:
    live = make_settings(generation_mode="live")
    await seed(engine, live)

    async with asgi_client(create_app(live)) as http:
        response = await http.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == []


# --- Seed --------------------------------------------------------------------


async def test_reseeding_is_idempotent(migrated_database: str, engine: AsyncEngine) -> None:
    settings = make_settings()
    await seed(engine, settings)
    first = (await _listed(settings)).json()

    await seed(engine, settings)
    second = (await _listed(settings)).json()

    assert first == second


async def test_reseeding_drops_a_model_that_is_no_longer_configured(
    migrated_database: str, engine: AsyncEngine
) -> None:
    """A changed chat slug must not leave the old one advertised.

    The row survives (chat history references it), but it stops being offered.
    """
    first = make_settings(ai_chat_model="chat-model-a", **CONFIGURED_CHAT)
    await seed(engine, first)
    assert "chat-model-a" in {model["id"] for model in (await _listed(first)).json()}

    second = make_settings(ai_chat_model="chat-model-b", **CONFIGURED_CHAT)
    async with session_scope(create_session_factory(engine)) as session:
        await upsert_models(session, second)

    listed = {model["id"] for model in (await _listed(second)).json()}
    assert "chat-model-b" in listed
    assert "chat-model-a" not in listed


async def test_a_retired_model_still_exists_in_the_table(
    migrated_database: str, engine: AsyncEngine
) -> None:
    """Retiring must not delete: old chats name the model that answered them."""
    first = make_settings(ai_chat_model="chat-model-a", **CONFIGURED_CHAT)
    await seed(engine, first)

    second = make_settings(ai_chat_model="chat-model-b", **CONFIGURED_CHAT)
    async with session_scope(create_session_factory(engine)) as session:
        await upsert_models(session, second)

    async with session_scope(create_session_factory(engine)) as session:
        retired = await session.get(Model, "chat-model-a")

    assert retired is not None
    assert retired.available is False


async def _listed(settings: Settings) -> httpx.Response:
    async with asgi_client(create_app(settings)) as http:
        return await http.get("/v1/models")


async def test_cli_seed_writes_the_catalog(migrated_database: str, engine: AsyncEngine) -> None:
    """`python -m app.cli seed` refreshes both catalogs from the environment."""
    settings = get_settings()

    await seed_catalog()

    async with asgi_client(create_app(settings)) as http:
        listed = {model["id"] for model in (await http.get("/v1/models")).json()}
        plans = (await http.get("/v1/plans")).json()

    expected = {IMAGE_MODEL_ID, VIDEO_MODEL_ID} if settings.generation_mode == "mock" else set()
    if settings.chat_provider_configured:
        expected.add(settings.ai_chat_model)
    assert listed == expected
    assert [plan["id"] for plan in plans] == ["free", "pro", "studio"]
