"""Public API for explicit Claude and Codex settings imports.

Normal Fluid discovery never reads provider homes. Hosts and the CLI use this
module to preview and selectively copy portable settings into Fluid-owned paths.
"""

from fluid.context.settings_importer import (
    SettingsImportItem,
    SettingsImportPreview,
    SettingsImportResult,
    apply_settings_import,
    import_settings,
    preview_settings_import,
)

__all__ = [
    "SettingsImportItem",
    "SettingsImportPreview",
    "SettingsImportResult",
    "apply_settings_import",
    "import_settings",
    "preview_settings_import",
]
