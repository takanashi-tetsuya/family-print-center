"""Shared headless Office environment, usable by Debian's system Python UNO."""
import os


# Documents never need web/admin credentials. An inherited loader hook can also
# break the preview process or load an old native-job bridge into unrelated jobs.
PRIVATE_ENVIRONMENT = {"ADMIN_PASSWORD", "SECRET_KEY", "CUPS_PASSWORD_FILE", "LD_PRELOAD"}


def office_environment(**extra):
    environment = {key: value for key, value in {**os.environ, **extra}.items()
                   if key not in PRIVATE_ENVIRONMENT}
    # --headless with the gen backend still attempts an X11 connection. svp is
    # the display-independent renderer required by the router/container runtime.
    environment["SAL_USE_VCLPLUGIN"] = "svp"
    return environment
