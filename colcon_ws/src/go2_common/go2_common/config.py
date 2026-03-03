"""
config.py — Robot configuration for Unitree Go2 packages.

Configuration is read from (in priority order):
  1. Environment variables (e.g. ``GO2_NETWORK_INTERFACE``)
  2. ``config/robot.yaml`` in the workspace root directory

The workspace root is auto-detected by walking up from this file's location
until a directory containing ``config/robot.yaml`` or
``config/robot.yaml.template`` is found.

Setup::

    cp config/robot.yaml.template config/robot.yaml
    # then edit config/robot.yaml with your actual settings

Usage::

    from go2_common.config import load_config
    cfg = load_config()
    print(cfg.network_interface)   # 'eth0' or None
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Typed config container
# ---------------------------------------------------------------------------

@dataclass
class RobotConfig:
    """All robot configuration values, loaded from ``config/robot.yaml``.

    Add new fields here as the project grows; extend ``_parse_config()`` to
    populate them from the YAML file and environment variables.
    """
    network_interface: Optional[str]


# ---------------------------------------------------------------------------
# Workspace root detection
# ---------------------------------------------------------------------------

def _find_workspace_root() -> Optional[Path]:
    """Walk up directory tree from this file to find the workspace root.

    The workspace root is the first ancestor directory that contains a
    ``config/`` subdirectory with either ``robot.yaml`` or
    ``robot.yaml.template`` inside it.

    Returns ``None`` if no such directory is found.
    """
    current = Path(__file__).resolve().parent
    for _ in range(15):  # hard limit to avoid infinite traversal
        cfg_dir = current / 'config'
        if (cfg_dir / 'robot.yaml').exists() or (cfg_dir / 'robot.yaml.template').exists():
            return current
        parent = current.parent
        if parent == current:   # reached filesystem root
            break
        current = parent
    return None


def _config_file() -> Optional[Path]:
    """Return the path to ``config/robot.yaml``, or ``None`` if workspace
    root cannot be found."""
    root = _find_workspace_root()
    return (root / 'config' / 'robot.yaml') if root else None


# Resolved once at import time.
_WORKSPACE_ROOT: Optional[Path] = _find_workspace_root()
CONFIG_FILE: Optional[Path] = (
    _WORKSPACE_ROOT / 'config' / 'robot.yaml' if _WORKSPACE_ROOT else None
)


# ---------------------------------------------------------------------------
# Low-level file I/O  (no external YAML library required)
# ---------------------------------------------------------------------------

def _read_yaml_file() -> dict:
    """Read ``config/robot.yaml`` and return a plain dict.

    Returns an empty dict if the file does not exist or cannot be parsed.
    Supports only top-level ``key: value`` pairs; comments (``#``) and blank
    lines are ignored.
    """
    cfg = _config_file()
    if cfg is None or not cfg.exists():
        return {}
    data: dict = {}
    try:
        with cfg.open() as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    key, _, val = line.partition(':')
                    data[key.strip()] = val.strip()
    except OSError:
        pass
    return data


def _parse_config(raw: dict) -> RobotConfig:
    """Build a :class:`RobotConfig` from the raw dict, applying env overrides."""

    def _get(env_key: str, yaml_key: str) -> Optional[str]:
        """Return the env var if set, otherwise the YAML value, or ``None``."""
        val = os.environ.get(env_key, '').strip() or raw.get(yaml_key, '').strip()
        return val if val else None

    return RobotConfig(
        network_interface=_get('GO2_NETWORK_INTERFACE', 'network_interface'),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config() -> RobotConfig:
    """Load the robot configuration and return a :class:`RobotConfig`.

    Values are sourced from environment variables first, then from
    ``config/robot.yaml`` in the workspace root.

    This is the **primary entry point** for all packages::

        from go2_common.config import load_config

        cfg = load_config()
        print(cfg.network_interface)  # e.g. 'eth0' or None
    """
    return _parse_config(_read_yaml_file())


def save_config(data: dict) -> None:
    """Merge *data* into ``config/robot.yaml``, creating it if necessary.

    Existing keys not present in *data* are preserved.
    """
    cfg = _config_file()
    if cfg is None:
        raise RuntimeError(
            'Cannot locate workspace root — save_config() requires the package '
            'to be run from inside the Go2 workspace directory tree.'
        )
    existing = _read_yaml_file()
    existing.update(data)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with cfg.open('w') as fh:
        fh.write('# Unitree Go2 robot configuration\n')
        fh.write('# Edit this file or use go2_common.config.save_config()\n\n')
        for key, val in existing.items():
            fh.write(f'{key}: {val}\n')


# ---------------------------------------------------------------------------
# Convenience shim (backward compatibility)
# ---------------------------------------------------------------------------

def get_network_interface() -> Optional[str]:
    """Return the configured network interface.

    .. deprecated::
        Call :func:`load_config` and use ``cfg.network_interface`` instead.
    """
    return load_config().network_interface
