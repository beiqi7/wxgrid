"""wxgrid — township-resolvable forecast pipeline over ECMWF Open Data + NOAA GFS.

Canonical internal representation
--------------------------------
Every source normalises to the same :class:`ForecastGrid`:

    dims    : step (int hours), latitude, longitude
    t2m     : 2 m air temperature, degC
    u10/v10 : 10 m wind components, m/s
    tp      : precipitation ACCUMULATED FROM INIT, mm
    orog    : model grid orography, m (static, 2-D)
    coords  : init_time, step, valid_time, latitude (desc), longitude (asc, -180..180)
"""

from .grid import ForecastGrid
from .runs import Run

__all__ = ["ForecastGrid", "Run"]
__version__ = "0.1.0"
