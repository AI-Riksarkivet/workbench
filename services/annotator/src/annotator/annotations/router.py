"""The annotations group router — composes the feature modules under ``/api``.

ORDER IS MATCHING: a unit key is a path (``{key:path}``), so ``/annotations/<key>`` would also match
``/annotations/<key>/versions``, ``/annotations/<key>/revert``, ``/annotations/<key>/proposals`` and
``/annotations/tags``. Starlette takes the first route
that matches, so the more specific routes are registered first (versions before the unit read; tags, revert and
proposals before the unit save).
"""

from fastapi import APIRouter

from annotator.annotations import proposals, revert, save, tags, versions, wire


router = APIRouter(prefix="/api")
router.include_router(versions.router)
router.include_router(wire.router)
router.include_router(tags.router)
router.include_router(revert.router)
router.include_router(proposals.router)
router.include_router(save.router)
