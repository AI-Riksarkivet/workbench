"""What two or more of the workbench's services share.

The app factory every service is built by (``workbench.app``) and what it installs; sign-in, authorization and the
audit trail (``workbench.governed``); the declared sources (``workbench.sources``); the dataset registry and the
descriptor it serves (``workbench.lancekit``); and the media services' settings, lifespan and readiness
(``workbench.media``). Code one service alone uses lives in that service.
"""
