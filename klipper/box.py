"""Klipper entry point with identity/telemetry-only admission startup.

Deploy this file as extras/box.py and the implementation as extras/cfs/.
Host mechanics remain blocked; printer acceptance is separate from PC checks.
"""

from .cfs.box import load_config
