# Data sources and licences

NarSat's code is licensed under Apache-2.0. The data it fetches and the model it uses
keep their own terms. This page summarises how each source is used; **read the
provider's own terms before publishing results**, as they can change.

| Source | Used for | Access | Terms to respect |
| --- | --- | --- | --- |
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) | Active-fire detections (VIIRS S-NPP, NOAA-20, NOAA-21; MODIS) | Area API with a free MAP_KEY; 5,000 transactions per 10 minutes | NASA open data; FIRMS asks users to acknowledge it as the source (see its citation guidance). The map displays the attribution. |
| Copernicus Sentinel-2 L2A via [Earth Search](https://earth-search.aws.element84.com/v1) (Element 84) | Pre- and post-fire surface reflectance, scene classification | Public STAC API, collection `sentinel-2-c1-l2a`; Cloud-Optimized GeoTIFFs read over HTTPS; no account | Free and open Copernicus data. Products derived from it carry "Contains modified Copernicus Sentinel data", shown on the map. |
| [Prithvi-EO-2.0-300M-BurnScars](https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars) (IBM, NASA) | Burn-scar segmentation | Hugging Face Hub, public, about 1.3 GB, downloaded by the inference service | Apache-2.0. Cite the model as its model card asks. Fine-tuned on burn scars in the United States. |
| [geoBoundaries](https://www.geoboundaries.org/) (gbOpen) | Country and first-level administrative units | Public API, no account | The licence of each boundary set is returned by the API and stored with it (`admin_unit.licence`); attribute accordingly. |
| [EFFIS](https://forest-fire.emergency.copernicus.eu/) burnt areas (optional) | Reference perimeters for agreement metrics | Manual download after accepting the portal's terms | Use under the EFFIS terms accepted at download; not redistributed by NarSat. |
| [NEALGEBA v1.0](https://doi.org/10.5281/zenodo.10684711) (Kouachi, Khairoun, Baeza, Moutahir, 2024) | Independent reference for accuracy: Landsat burned areas of north-east Algeria, 1984–2023 | Public download from Zenodo, no account (`NEALGEBA_v1.0.zip`, MD5 `7634f4a283be19b8e9e12dbdf363d700`) | CC BY 4.0: cite the dataset and the paper (Remote Sensing 16(13), 2500, 2024). Not redistributed by NarSat. |
| [OpenFreeMap](https://openfreemap.org/) | Base map in the web application | Public tiles, no key | Map data © OpenStreetMap contributors (ODbL); the attribution is part of the style and shown on the map. |

## What the repository contains

* No downloaded data. `tests/fixtures/` holds a few hand-written FIRMS-format CSV rows
  used to test the parser, and `services/inference/tests/fixtures/` a model architecture
  description used to build the network with random weights in tests.
* The end-to-end tests generate **synthetic** rasters (uniform vegetation with a burned
  disc) and synthetic detections. They exercise the software; they are not observations
  and must not be presented as results.

## Notes on use

* FIRMS detections are thermal anomalies in pixels of 375 m (VIIRS) or 1 km (MODIS). A
  detection does not mean a whole pixel burned, and some are not vegetation fires.
* Sentinel-2 L2A reflectance is scaled with the `raster:bands` scale and offset published
  in each STAC item (the 2022 processing baseline introduced an offset), so values are
  physical reflectance whatever the baseline.
* geoBoundaries' open (gbOpen) boundaries for Algeria, Morocco and Tunisia are derived
  from OpenStreetMap and licensed under the ODbL 1.0 (as returned by the API in November
  2026). The Algerian set dates from 2017 and has 48 wilayas, before the reforms that
  created new ones; a newer official layer can be loaded from a file with
  `narsat reference load-admin --path`.
* Areas are computed on the WGS84 ellipsoid (pyproj `Geod` for products, PostGIS
  `geography` for agreement metrics), not in degrees.
* NEALGEBA was made by its authors from Landsat with a semi-automatic Random Forest
  method and visual checks, so it is an independent satellite product, not ground truth.
  It covers six coastal wilayas: Annaba, Béjaïa, El Tarf, Jijel, Skikda and Tizi Ouzou.
  Its dates are detection dates, which the paper reports lag the fire by 42.55 days on
  average. NarSat therefore matches it by place (`--no-date`), and evaluates only inside
  those wilayas.
