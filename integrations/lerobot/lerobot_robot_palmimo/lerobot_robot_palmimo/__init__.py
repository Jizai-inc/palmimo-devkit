from .motor_layout import register_xc330_m288


register_xc330_m288()

from .config_palmimo import PalmimoConfig
from .palmimo import Palmimo


__all__ = ["Palmimo", "PalmimoConfig"]
