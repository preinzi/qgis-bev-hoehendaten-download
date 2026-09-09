# BEV Höhendaten Bulk-Download (bundesweit)

A QGIS Processing script that bulk-downloads elevation raster tiles (DGM/DOM, 1m) from the official BEV (Bundesamt für Eich- und Vermessungswesen) nationwide data catalog for a chosen area, anywhere in Austria, and adds ready-to-use mosaic layers straight to your project.

> **Note:** The tool's interface (parameter labels, log messages, help text) is in German, matching its target audience. This README is in English for discoverability.

<img src="images/screenshot.png" width="400" alt="Screenshot of the tool in QGIS">

## What it does

Given an area of interest anywhere in Austria, this tool:

- Computes which of BEV's nationwide 50×50 km tiles cover your area directly from the coordinates — no catalog search needed, since the tile grid is fixed and the tile IDs encode their own position.
- Reads only the actual bytes it needs via HTTP range requests directly from BEV's Cloud-Optimized GeoTIFFs (`/vsicurl/`), instead of downloading the full tile — relevant because a single tile can be several GB. Falls back to downloading (and locally caching) the complete tile only if the windowed read fails for some reason.
- Automatically finds the most recent available edition of each tile, since BEV re-publishes its nationwide mosaic yearly but not every tile is updated in every edition.
- Lets you pick which elevation model type(s) to fetch — DGM (terrain model) and/or DOM (surface model) — each producing its own mosaic layer.
- Builds a lightweight VRT mosaic per model type (no pixel duplication on disk) and adds it directly to your QGIS project. The CRS is read directly from a real downloaded piece rather than assumed.
- Optional on-the-fly reprojection to a target CRS of your choice, via a standard CRS picker; warns you if the best available system transformation is less accurate than the data's own 1m resolution (usually caused by a missing PROJ datum grid).
- Cancellable mid-run, including a download or windowed read already in progress; failed tiles are reported in the log rather than silently skipped.

### Note on coordinate reference systems

BEV's data is delivered in **EPSG:3035** (ETRS89-extended / LAEA Europe), a single nationwide projection — no zone boundaries to worry about, unlike Land-level services that split Austria into several Gauss-Krüger zones. The tool still reads the CRS directly from a real downloaded piece rather than hard-coding it, to catch the rare case of it ever being wrong.

## Installation

This is a single-file **Processing script**, not a full plugin:

1. Download [`bev_hoehendaten_bulk_download.py`](https://github.com/preinzi/qgis-bev-hoehendaten-download/blob/main/bev_hoehendaten_bulk_download.py).
2. In QGIS: **Processing → Toolbox → Scripts (gear icon) → Add Script to Toolbox…**, and select the file.
3. It will appear under **Skripte/Scripts → BEV → BEV Höhendaten Bulk-Download (bundesweit)**.

No extra Python packages required beyond what ships with QGIS (uses only the Python standard library plus the bundled GDAL/PyQGIS). Optionally, if [`pyproj`](https://pypi.org/project/pyproj/) is installed, the tool also warns you about the accuracy of a chosen target-CRS reprojection — but this is not a hard requirement. Tested on QGIS 3.34 LTR (Linux) and QGIS 3.44 LTR (Windows).

## Usage

| Parameter         | Description                                                                                                                                                                                              |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Gebiet (AOI)**  | Area of interest, anywhere in Austria — draw a rectangle, use the current canvas extent or calculate extent from a layer                                                                                 |
| **Modelltyp(en)** | Which elevation model(s) to fetch: DGM and/or DOM                                                                                                                                                        |
| **Ziel-CRS**      | Optional. Leave empty to keep the data's own EPSG:3035; pick a CRS to get an additional, virtually reprojected VRT (resampling method is cubic)                                                          |
| **Zielordner**    | Where downloaded tiles and mosaics are stored. It is recommended to use a persistent folder, not the default temp location — results should stay usable after the QGIS session ends, and a later run over an overlapping area reuses already-downloaded pieces instead of re-fetching them |

### Output layers

For every selected model type, one layer is added to the project (`DGM` and/or `DOM`). On disk, each gets its own subfolder under the chosen output folder, containing the clipped tile piece(s) plus a `..._mosaic.vrt` (and, if a target CRS was chosen, an additional reprojected VRT). A `_cache` subfolder holds any full tiles downloaded via the fallback path, reused across runs.

## Data source

Quelle: Bundesamt für Eich- und Vermessungswesen (BEV), <https://www.bev.gv.at>, CC-BY-4.0

## License

GPL-3.0-or-later — see [LICENSE](https://github.com/preinzi/qgis-bev-hoehendaten-download/blob/main/LICENSE).

## Credits

Written by Stephan Preinstorfer (LiberGIS) with help from Claude (Anthropic).

## Contributing

Issues and pull requests welcome.
