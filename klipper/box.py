"""Klipper entry point for the normal CFS host workflow.

Deploy this file as extras/box.py and the implementation as extras/cfs/.
Set passive: true in [box] to select the diagnostic lifecycle.
"""

from .cfs.box import load_config as _load_config


def load_config(config):
    return _load_config(config, passive=config.getboolean("passive", False))
