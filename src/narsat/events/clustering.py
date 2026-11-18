"""Spatio-temporal clustering of thermal detections.

Two detections are neighbours when they are within ``eps_km`` of each other on the
ground *and* within ``eps_hours`` in time. Clusters are then formed with DBSCAN over
that neighbour graph, which is the ST-DBSCAN formulation of Birant & Kut (2007) with
a single density threshold.

Implementation: a haversine ball tree finds the spatial neighbours, pairs that are
too far apart in time are dropped, and scikit-learn's DBSCAN runs on the resulting
sparse distance matrix (``metric="precomputed"``). With ``min_samples=1`` this is
exactly the connected components of the neighbour graph (single linkage), the
behaviour used by satellite fire-tracking systems; larger values additionally discard
isolated detections as noise.
"""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp
from sklearn.cluster import DBSCAN
from sklearn.neighbors import BallTree

from narsat.geo import EARTH_RADIUS_KM

NOISE = -1


def st_dbscan(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    hours: np.ndarray,
    eps_km: float,
    eps_hours: float,
    min_samples: int = 1,
) -> np.ndarray:
    """Cluster detections; returns one integer label per input row (``-1`` is noise).

    ``hours`` is acquisition time expressed in hours on any common origin.
    """
    lat = np.asarray(latitudes, dtype="float64")
    lon = np.asarray(longitudes, dtype="float64")
    t = np.asarray(hours, dtype="float64")
    if not (lat.shape == lon.shape == t.shape) or lat.ndim != 1:
        raise ValueError("latitudes, longitudes and hours must be 1-D arrays of equal length")
    n = lat.size
    if n == 0:
        return np.empty(0, dtype="int64")
    if eps_km <= 0 or eps_hours <= 0 or min_samples < 1:
        raise ValueError("eps_km and eps_hours must be positive and min_samples >= 1")

    coords = np.deg2rad(np.column_stack([lat, lon]))
    tree = BallTree(coords, metric="haversine")
    neighbours, distances = tree.query_radius(
        coords, r=eps_km / EARTH_RADIUS_KM, return_distance=True
    )

    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    for i in range(n):
        idx = neighbours[i]
        keep = np.abs(t[idx] - t[i]) <= eps_hours
        idx = idx[keep]
        rows.append(np.full(idx.size, i, dtype="int64"))
        cols.append(idx.astype("int64"))
        vals.append(distances[i][keep] * EARTH_RADIUS_KM)
    # Coincident detections have distance 0. The zeros must stay *stored* in the sparse
    # matrix: scikit-learn treats missing entries, not zero entries, as "not neighbours".
    graph = sp.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)
    )
    labels: np.ndarray = (
        DBSCAN(eps=eps_km, min_samples=min_samples, metric="precomputed").fit(graph).labels_
    )
    return labels.astype("int64")
