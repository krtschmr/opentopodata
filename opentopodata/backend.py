import collections
import math
import os
import re
from glob import glob

import numpy as np
import pyproj
import rasterio
from rasterio.enums import Resampling


# Only a subset of rasterio's supported methods are currently activated. In
# the future I might do interpolation in backend.py instead if relying on
# gdal, and I don't want to commit to supporting an interpolation method that
# would be a pain to do in python.
INTERPOLATION_METHODS = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    # 'cubic_spline': Resampling.cubic_spline,
    # 'lanczos': Resampling.lanczos,
}


WGS84_LATLON_EPSG = 4326


class InputError(ValueError):
    """Invalid input data.

    The error message should be safe to pass back to the client.
    """

    pass


def _noop(x):
    return x


def _reproject_latlons(lats, lons, epsg):
    """Convert WGS84 latlons to another projection.

    Args:
        lats, lons: Lists/arrays of latitude/longitude numbers.
        epsg: Integer EPSG code.

    """
    if epsg == WGS84_LATLON_EPSG:
        return lons, lats

    # Validate EPSG.
    if not 1024 <= epsg <= 32767:
        raise InputError("Dataset has invalid projection.")

    # Do the transform. Pyproj assumes EPSG:4326 as default source projection.
    projection = pyproj.Proj(init=f"EPSG:{epsg}")
    x, y = projection(lons, lats)

    return x, y


def _validate_points_lie_within_raster(xs, ys, lats, lons, bounds, res):
    """Check that querying the dataset won't throw an error.

    Args:
        xs, ys: Lists/arrays of x/y coordinates, in projection of file.
        lats, lons: Lists/arrays of lat/lon coordinates. Only used for error message.
        bounds: rastio BoundingBox object.
        res: Tuple of (x_res, y_res) resolutions.

    Raises:
        InputError: if one of the points lies outside bounds.
    """

    # Get actual extent. When storing point data in a pixel-based raster
    # format, the true extent is the centre of the outer pixels, but GDAL
    # reports the exent as the outer edge ouf the outer pixels. So need to
    # adjust by half the pixel width.
    x_min = min(bounds.left, bounds.right) + res[0] / 2
    x_max = max(bounds.left, bounds.right) - res[0] / 2
    y_min = min(bounds.top, bounds.bottom) + res[1] / 2
    y_max = max(bounds.top, bounds.bottom) - res[1] / 2

    # Check bounds.
    x_in_bounds = (xs >= x_min) & (xs <= x_max)
    y_in_bounds = (ys >= y_min) & (ys <= y_max)

    # Raise exception if out of bounds.
    if not all(y_in_bounds):
        i_oob = np.argmax(y_in_bounds)
        lat = lats[i_oob]
        lon = lons[i_oob]
        msg = "Location '{},{}' has latitude outside of raster bounds".format(lat, lon)
        raise InputError(msg)
    if not all(x_in_bounds):
        i_oob = np.argmax(x_in_bounds)
        lat = lats[i_oob]
        lon = lons[i_oob]
        msg = "Location '{},{}' has longitude outside of raster bounds".format(lat, lon)
        raise InputError(msg)


def _get_elevation_from_path(lats, lons, path, interpolation):
    """Read values at locations in a raster.

    Args:
        lats, lons: Arrays of latitudes/longitudes.
        path: GDAL supported raster location.
        interpolation: method name string.

    Returns:
        z_all: List of elevations, same length as lats/lons.
    """
    z_all = []
    interpolation = INTERPOLATION_METHODS.get(interpolation)
    lons = np.asarray(lons)
    lats = np.asarray(lats)

    with rasterio.open(path) as f:
        xs, ys = _reproject_latlons(lats, lons, f.crs.to_epsg())

        # Check bounds.
        _validate_points_lie_within_raster(xs, ys, lats, lons, f.bounds, f.res)
        rows, cols = tuple(f.index(xs, ys, op=_noop))

        # Offset by 0.5 to convert from center coords (provided by
        # f.index) to ul coords (expected by f.read).
        rows = np.array(rows) - 0.5
        cols = np.array(cols) - 0.5

        # Because of floating point precision, indices may slightly exceed
        # array bounds. Because we've checked the locations are within the
        # file bounds,  it's safe to clip to the array shape.
        rows = rows.clip(0, f.height - 1)
        cols = cols.clip(0, f.width - 1)

        # Read the locations, using a 1x1 window. The `masked` kwarg makes
        # rasterio replace NODATA values with np.nan. The `boundless` kwarg
        # forces the windowed elevation to be a 1x1 array, even when it all
        # values are NODATA.
        for row, col in zip(rows, cols):
            window = rasterio.windows.Window(col, row, 1, 1)
            z_array = f.read(
                indexes=1,
                window=window,
                resampling=interpolation,
                out_dtype=float,
                boundless=True,
                masked=True,
            )
            z = np.ma.filled(z_array, np.nan)[0][0]
            z_all.append(z)
    return z_all


def get_elevation(lats, lons, dataset, interpolation="nearest"):
    """Read elecations from a dataset.

    A dataset may consist of multiple files, so need to determine which
    locations lies in which file, then loop over the files.

    Args:
        lats, lons: Arrays of latitudes/longitudes.
        dataset: config.Dataset object.
        interpolation: method name string.

    Returns:
        elevations: List of elevations, same length as lats/lons.
    """

    # Which paths we need results from.
    lats = np.array(lats)
    lons = np.array(lons)
    paths = dataset.location_paths(lats, lons)

    # Store mapping of tile path to point so we can merge back together later.
    elevations_by_path = {}
    path_to_point_index = collections.defaultdict(list)
    for i, path in enumerate(paths):
        path_to_point_index[path].append(i)

    # Check if a path wasn't found.
    if None in path_to_point_index:
        indices = path_to_point_index[None]
        no_path_lats = lats[indices]
        no_path_lons = lons[indices]
        fill_values = dataset.missing_tile_elevations(no_path_lats, no_path_lons)
        if None not in fill_values:
            elevations_by_path[None] = fill_values
        else:
            i = fill_values.index(None)
            msg = "Point '{},{}' is outside dataset bounds.".format(
                no_path_lats[i], no_path_lons[i]
            )
            raise InputError(msg)

    # Batch results by path.
    for path, indices in path_to_point_index.items():
        if path is None:
            continue
        batch_lats = lats[path_to_point_index[path]]
        batch_lons = lons[path_to_point_index[path]]
        elevations_by_path[path] = _get_elevation_from_path(
            batch_lats, batch_lons, path, interpolation
        )

    # Put the results back again.
    elevations = [None] * len(paths)
    for path, path_elevations in elevations_by_path.items():
        for i_path, i_original in enumerate(path_to_point_index[path]):
            elevations[i_original] = path_elevations[i_path]

    return elevations