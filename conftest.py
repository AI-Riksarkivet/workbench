"""What every offline suite in the workspace shares.

At import, before collection builds any app, the media plane is pointed at an empty lance-namespace directory catalog
under the system temp dir (``MEDIA_CATALOG_DIR``, with ``setdefault``): the media settings refuse to load naming no
catalog, and the offline suites run on the stock ``dir`` catalog the media code runs on unchanged. A test whose claim is
about the datasets builds them under its own ``tmp_path`` (``tests.media_catalog``) and names that root in the settings
it builds.
"""

from __future__ import annotations

import os
import tempfile


os.environ.setdefault("MEDIA_CATALOG_DIR", tempfile.mkdtemp(prefix="workbench-media-catalog-"))
