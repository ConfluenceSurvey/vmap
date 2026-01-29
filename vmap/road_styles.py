"""Road classification styles: line weights and ACI colors."""

from dataclasses import dataclass

@dataclass(frozen=True)
class RoadStyle:
    lineweight: int   # hundredths of mm
    color: int        # AutoCAD Color Index
    label_height: float  # text height in drawing units


# Keyed by OSM highway tag value.
STYLES: dict[str, RoadStyle] = {
    "motorway":       RoadStyle(80, 1, 0),  # red
    "motorway_link":  RoadStyle(70, 1, 0),
    "trunk":          RoadStyle(80, 1, 0),
    "trunk_link":     RoadStyle(70, 1, 0),
    "primary":        RoadStyle(60, 3, 0),  # green
    "primary_link":   RoadStyle(50, 3, 0),
    "secondary":      RoadStyle(45, 5, 0),  # blue
    "secondary_link": RoadStyle(40, 5, 0),
    "tertiary":       RoadStyle(35, 4, 0),  # cyan
    "tertiary_link":  RoadStyle(30, 4, 0),
    "residential":    RoadStyle(20, 7, 0),  # white
    "unclassified":   RoadStyle(15, 8, 0),  # gray
}

# label_height is set to 0 here; dxf_builder computes it from map extent.

DEFAULT_STYLE = RoadStyle(15, 8, 0)


def get_style(highway: str) -> RoadStyle:
    return STYLES.get(highway, DEFAULT_STYLE)
