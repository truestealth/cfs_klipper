"""Klipper integration: configuration, lifecycle, commands, and status access.

The bootstrap deliberately refuses activation until printer behavior is
implemented and verified. It must not obtain hardware objects or register
commands while the package is only a development scaffold.
"""


def load_config(config):
    """Reject activation before looking up or changing any printer objects."""
    raise config.error(
        "cfs_box: development scaffold; printer operation is not implemented."
    )
