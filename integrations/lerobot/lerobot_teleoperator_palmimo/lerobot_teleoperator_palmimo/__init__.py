try:
    from .config_palmimo import PalmimoTeleopConfig
    from .palmimo import PalmimoTeleop

    __all__ = ["PalmimoTeleop", "PalmimoTeleopConfig"]
except ModuleNotFoundError as error:
    # Only an absent lerobot is tolerated; any other missing module is a real defect.
    if error.name != "lerobot":
        raise
    __all__ = []
