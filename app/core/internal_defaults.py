"""Internal shipped policy. No environment sources or persisted overrides.

Area-owned policies still in config.Settings move here in PRs 2–6.
"""

from typing import Final

NETWORK_TOKEN_BINDING_ENABLED: Final[bool] = True
FILE_LOGGING_ENABLED: Final[bool] = True
MOBILE_APP_ORIGIN: Final[str] = "https://appassets.androidplatform.net"
MOBILE_IOS_APP_ORIGIN: Final[str] = "aishuffle://app"
MOBILE_ACCOUNT_HUB_VERIFY_TTL_SECONDS: Final[float] = 90.0
MOBILE_ACCOUNT_HUB_GRACE_SECONDS: Final[float] = 1800.0
SINGLE_INSTALL_SWEEP_INTERVAL_SECONDS: Final[int] = 60
SINGLE_INSTALL_PEER_CHECK_SECONDS: Final[int] = 300
ERROR_LOG_CAPTURE_WARNINGS: Final[bool] = True
ERROR_LOG_MAX_WARNING_ROWS: Final[int] = 1000
