"""Klipper entry point for the development scaffold, which refuses activation.

The planned layout places this file in extras/box.py and the implementation in
extras/cfs_box/. This layout is tested on a PC; it is not a printer installer.
"""

from .cfs_box.box import load_config
