"""Route inventory and authentication-dependency assertions.

These tests fail closed. A new endpoint cannot ship until it is classified in
`tests.support` and, when protected, wired to `get_current_auth`, so a route
added without an auth dependency breaks the build instead of leaking data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute

from app.api.deps import get_current_auth
from app.main import create_app
from tests.support import (
    PATH_PARAM_SAMPLES,
    PROTECTED_ROUTES,
    PUBLIC_ROUTES,
    ROUTES,
    RouteCase,
    make_settings,
)

#: Routes Starlette and FastAPI mount themselves; they are not part of the API.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect"})
PATH_PARAM = re.compile(r"\{([^{}]+)\}")


def iter_api_routes(routes: Any) -> Iterator[APIRoute]:
    """Yield every `APIRoute`, descending into included routers.

    FastAPI wraps `include_router` output in a router object whose children live
    on `original_router` rather than `routes`, so both attributes are followed.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            yield from iter_api_routes(nested)


def dependency_calls(dependant: Any) -> Iterator[Any]:
    """Yield the callable behind a dependency and every sub-dependency."""
    yield dependant.call
    for nested in dependant.dependencies:
        yield from dependency_calls(nested)


def find_route(api_routes: list[APIRoute], case: RouteCase) -> APIRoute:
    for route in api_routes:
        if route.path == case.path and case.method in route.methods:
            return route
    raise AssertionError(f"no route serves {case.method} {case.path}")


@pytest.fixture(scope="module")
def api_routes() -> list[APIRoute]:
    # No lifespan: the inventory tests only inspect the route table.
    return list(iter_api_routes(create_app(make_settings()).routes))


def test_every_route_is_classified(api_routes: list[APIRoute]) -> None:
    discovered = {
        (method, route.path) for route in api_routes for method in route.methods - {"HEAD"}
    }
    documented = {(case.method, case.path) for case in ROUTES}

    assert discovered == documented, (
        "The route table changed. Add the new route to PUBLIC_ROUTES or "
        "PROTECTED_ROUTES in tests/support.py and, when protected, give it a "
        "`get_current_auth` dependency."
    )


def test_only_framework_routes_escape_the_table() -> None:
    resolved = create_app(make_settings())
    others = {
        route.path
        for route in resolved.routes
        if not isinstance(route, APIRoute) and getattr(route, "path", None)
    }

    assert others == FRAMEWORK_PATHS


@pytest.mark.parametrize("case", PROTECTED_ROUTES, ids=lambda case: f"{case.method} {case.path}")
def test_protected_routes_depend_on_get_current_auth(
    api_routes: list[APIRoute], case: RouteCase
) -> None:
    route = find_route(api_routes, case)

    assert get_current_auth in set(dependency_calls(route.dependant)), (
        f"{case.method} {case.path} is listed as protected but does not depend on get_current_auth."
    )


@pytest.mark.parametrize("case", PUBLIC_ROUTES, ids=lambda case: f"{case.method} {case.path}")
def test_public_routes_do_not_require_auth(api_routes: list[APIRoute], case: RouteCase) -> None:
    route = find_route(api_routes, case)

    assert get_current_auth not in set(dependency_calls(route.dependant))


def test_protected_cases_declare_anonymous_unauthorized() -> None:
    assert {case.anonymous_status for case in PROTECTED_ROUTES} == {401}
    assert {case.anonymous_code for case in PROTECTED_ROUTES} == {"unauthorized"}


def test_every_path_parameter_has_a_sample_value() -> None:
    params = {name for case in ROUTES for name in PATH_PARAM.findall(case.path)}

    assert params <= set(PATH_PARAM_SAMPLES)
