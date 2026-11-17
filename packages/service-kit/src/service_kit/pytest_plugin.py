"""The platform's test seam, a pytest plugin a client's suite gets with `service-kit` ([[XC-190]]).

A client pins `service-kit` by lakehouse tag and runs its suite in its own repository, so what that suite needs to stay
hermetic against the platform's process-global state travels with the package, under the `pytest11` entry point
`service_kit`, and loads in any environment that installs the package; this repository's suites get the same seam the
same way: respx's bare `@respx.mock` is strict, and `respx_allows_unused_routes` opts a test out.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


try:
    import respx
except ImportError:  # a client whose suite mocks no HTTP does not install respx
    respx = None

# respx's own default is strict (`MockRouter.__init__` takes assert_all_called=True), and that holds for the
# `respx_mock` fixture and the called decorator form, but the bare `@respx.mock` is a module-level router built with
# `_assert_all_called = False`, so a route registered for a call the code no longer makes passes silently. Measured
# when first switched on in this repository: 17 of 227 tests across 16 respx files went red, thirteen of them mocking a
# namespace-create the cascade never makes and the real catalog answers 400. Flipping the one instance covers every
# bare use; `@respx.mock(assert_all_called=...)` would build a new router, and a module-level `respx.post(...)` in the
# test body would then register on the global one. To assert a call is not made, register no route for it
# (`assert_all_mocked` already raises on an unregistered call) or assert over `respx.calls`.
if respx is not None:
    respx.mock._assert_all_called = True


@pytest.fixture
def respx_allows_unused_routes() -> Iterator[None]:
    """Opt out of the strict default, for a test whose uncalled route is the point.

    A dead route (the code stopped making the call) is what the default catches. A negative route is registered so the
    test can prove it was not called, or which subset fired, and deleting it would delete the proof: taking this
    fixture declares a test of the second kind. A fixture rather than a decorator argument, because
    `@respx.mock(assert_all_called=False)` builds a new router and module-level `respx.get(...)` in the body would
    register on the global one.
    """
    if respx is None:
        pytest.fail("respx_allows_unused_routes needs respx installed")
    respx.mock._assert_all_called = False
    try:
        yield
    finally:
        respx.mock._assert_all_called = True
