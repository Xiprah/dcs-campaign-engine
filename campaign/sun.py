"""Where the sun is: the daylight rule's one source of truth.

The NOAA Solar Calculator's algorithm (NOAA Global Monitoring Laboratory,
"NOAA Solar Calculator" and its spreadsheet, which implement the low-precision
solar coordinates of Jean Meeus, *Astronomical Algorithms*), written out in
the spreadsheet's own steps. NOAA gives it as good to about a minute in
sunrise and sunset for latitudes within +/-72 degrees, which the daylight rule
needs and Syria, at 33 to 38 degrees north, is well inside.

Pure arithmetic on a timestamp, standard library only: no clock is read and
no time zone database is consulted, so a war fought on two machines sees the
same sun. Time comes in as *local* seconds since 1970-01-01 00:00 on the
proleptic Gregorian calendar -- the convention the mission client already
uses for `hello.mission_start_epoch` -- and the theater's fixed UTC offset
turns it into the UTC instant the formula wants.
"""

from __future__ import annotations

import math

#: Seconds in a day. Local epoch seconds divided by this is the local day.
DAY = 86_400.0

#: Julian date of 1970-01-01 00:00 UTC.
_JD_UNIX_EPOCH = 2_440_587.5
#: Julian date of the J2000.0 epoch.
_JD_J2000 = 2_451_545.0

#: The sun's geometric elevation, in degrees, at the published instants of
#: sunrise and sunset: its centre 50 arcminutes below the horizon, being 16
#: for its semidiameter and 34 for standard atmospheric refraction. NOAA and
#: almanac tables define sunrise and sunset by this, so "daylight" here is
#: exactly the span between the times a published table gives.
SUNRISE_ELEVATION = -0.833


def solar_elevation(
    local_epoch: float, latitude: float, longitude: float, utc_offset: float
) -> float:
    """The sun's geometric elevation in degrees above the horizon.

    `local_epoch` is local civil seconds since 1970-01-01 00:00, `latitude`
    and `longitude` degrees north and east, `utc_offset` hours east of UTC.
    Geometric: no refraction is added, because SUNRISE_ELEVATION already
    accounts for it at the one elevation the engine asks about.
    """
    utc_seconds = local_epoch - utc_offset * 3600.0
    julian_day = _JD_UNIX_EPOCH + utc_seconds / DAY
    t = (julian_day - _JD_J2000) / 36_525.0  # Julian centuries since J2000

    mean_longitude = (280.46646 + t * (36_000.76983 + t * 0.0003032)) % 360.0
    mean_anomaly = 357.52911 + t * (35_999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(mean_anomaly)
    centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2.0 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3.0 * m) * 0.000289
    )
    true_longitude = mean_longitude + centre
    omega = math.radians(125.04 - 1934.136 * t)
    apparent_longitude = true_longitude - 0.00569 - 0.00478 * math.sin(omega)
    mean_obliquity = 23.0 + (
        26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0
    ) / 60.0
    obliquity = math.radians(mean_obliquity + 0.00256 * math.cos(omega))
    declination = math.asin(
        math.sin(obliquity) * math.sin(math.radians(apparent_longitude))
    )

    y = math.tan(obliquity / 2.0) ** 2
    l0 = math.radians(mean_longitude)
    equation_of_time = 4.0 * math.degrees(
        y * math.sin(2.0 * l0)
        - 2.0 * eccentricity * math.sin(m)
        + 4.0 * eccentricity * y * math.sin(m) * math.cos(2.0 * l0)
        - 0.5 * y * y * math.sin(4.0 * l0)
        - 1.25 * eccentricity * eccentricity * math.sin(2.0 * m)
    )  # minutes

    local_minutes = (local_epoch % DAY) / 60.0
    true_solar_minutes = (
        local_minutes + equation_of_time + 4.0 * longitude - 60.0 * utc_offset
    ) % 1440.0
    hour_angle = true_solar_minutes / 4.0 - 180.0

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(math.radians(hour_angle))
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    return 90.0 - zenith


def is_daylight(
    local_epoch: float, latitude: float, longitude: float, utc_offset: float
) -> bool:
    """Is the instant between sunrise and sunset, as an almanac would say?"""
    return (
        solar_elevation(local_epoch, latitude, longitude, utc_offset)
        >= SUNRISE_ELEVATION
    )


__all__ = ["DAY", "SUNRISE_ELEVATION", "is_daylight", "solar_elevation"]
