"""Defaults and validation for the two current delivery settings."""

DEFAULT_TIMING = {"normal_window": 90, "max_formal_batch": 20}


def resolve_timing(timing):
    if timing is None:
        return dict(DEFAULT_TIMING)
    if not isinstance(timing, dict):
        raise ValueError("timing must be an object")
    unknown = sorted(timing.keys() - DEFAULT_TIMING.keys())
    if unknown:
        raise ValueError("unknown timing keys: %s" % unknown)
    for key, value in timing.items():
        if type(value) is not int or value < 0:
            raise ValueError("timing.%s must be a non-negative integer" % key)
    return {**DEFAULT_TIMING, **timing}
