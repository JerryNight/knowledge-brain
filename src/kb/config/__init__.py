"""Configuration package.

Import `get_settings()` rather than a module-level instance — importing this
package must not require env vars to be present.
"""

from kb.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
