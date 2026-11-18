# NarSat inference service

A small HTTP service that turns a Sentinel-2 reflectance cube into a burn-scar
probability raster. It wraps **Prithvi-EO-2.0-300M-BurnScars** (IBM / NASA,
Apache-2.0), loaded with **TerraTorch**.

It is a separate service on purpose:

* the model stack (PyTorch, TerraTorch: about 140 packages) stays out of the pipeline's
  environment;
* the pipeline keeps working without it: the spectral-index (dNBR) method needs no
  model, and the foundation-model method is an additional, comparable product;
* on a MacBook it can run **natively with the Apple GPU (MPS)** while everything else
  runs in Docker, where no GPU is available.

## Contract

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | liveness; answers once the model is loaded |
| `GET /v1/model` | model name, version (commit of the weights), device, bands, tiling |
| `POST /v1/segment` | reflectance in, probability out |

`POST /v1/segment`

* **Request body**: a NumPy `.npy` array, floating point, shape `(6, height, width)`,
  bands in this order: `blue, green, red, nir08, swir16, swir22` (Sentinel-2 B02, B03,
  B04, B8A, B11, B12). Values are **surface reflectance in 0-1 units**; missing pixels
  are `NaN`. The model was trained on 30 m Harmonized Landsat Sentinel-2 data, so the
  caller resamples to 30 m (the pipeline does).
* **Response body**: a `.npy` array, `float32`, shape `(height, width)`: probability of
  the *burn scar* class. Pixels without input data get `0` (no evidence); the caller
  knows which pixels were missing and masks them itself. The response header
  `X-Inference-Seconds` reports the time spent in the model.
* **Errors**: `400` body is not a `.npy` array; `413` raster larger than the configured
  limit; `422` wrong shape, integer data, or values that look like digital numbers
  (0-10000) instead of reflectance.

Large rasters are processed in overlapping tiles (512 px, 64 px overlap by default)
blended with a smooth window, so tile borders do not show in the result. One request
is processed at a time, which keeps memory use predictable on a laptop.

## Run it natively on macOS (recommended on Apple Silicon)

```bash
cd services/inference
python3.13 -m venv .venv
.venv/bin/pip install ".[prithvi]"
PYTORCH_ENABLE_MPS_FALLBACK=1 NARSAT_INFERENCE_DEVICE=auto \
    .venv/bin/uvicorn narsat_inference.app:create_app --factory --host 127.0.0.1 --port 8090
```

Port 8090, because the web application already publishes 8080 on the host.
`PYTORCH_ENABLE_MPS_FALLBACK=1` lets PyTorch run on the CPU any operation the Apple GPU
does not support yet, instead of failing.

The first start downloads the configuration and the checkpoint (about 1.3 GB) from the
Hugging Face Hub into `~/.cache/huggingface`. No account or token is needed for this
public model. `device=auto` picks CUDA, then MPS, then CPU.

Measured on a MacBook Pro (Apple Silicon, 16 GB, MPS) for one fire in Bouira, Algeria
(July 2023): about 1.4 s from the model-information call to the segmentation answer; the
whole mapping of the event took 31 s, most of it downloading the Sentinel-2 images.

Then tell the pipeline where the service is (`.env` of the main project):

```
NARSAT_SEG_METHODS=dnbr,model
NARSAT_SEG_INFERENCE_URL=http://host.docker.internal:8090   # from containers
```

## Run it in Docker (CPU)

```bash
docker compose --profile ml up -d inference
```

A container on macOS cannot use the GPU. For scale, measured in the development
environment of this project (2 CPU threads, random weights of the real architecture):
loading the model peaks at about 3.6 GB of memory, and a 1024 x 1024 pixel request
(about 30 x 30 km at 30 m) took 34 s. A laptop with more cores is faster; the Apple GPU
(native run, MPS) much faster. Give Docker Desktop at least 12 GB with this profile.

## Run it without the model

```bash
NARSAT_INFERENCE_ENGINE=nbr uvicorn narsat_inference.app:create_app --factory --port 8090
```

The `nbr` engine is a single-date spectral baseline with the same contract. It needs
no weights and no PyTorch; it exists so that the integration between pipeline and
service can be exercised in CI and on machines that cannot hold the model. Its output
is **not** a model prediction and is labelled as engine `nbr` in every product it
produces.

## Settings

Environment variables, prefix `NARSAT_INFERENCE_`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENGINE` | `prithvi` | `prithvi` or `nbr` |
| `MODEL_REPO` | `ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars` | Hugging Face repository |
| `MODEL_REVISION` | `main` | pin a commit hash for reproducible results |
| `CONFIG_PATH`, `CHECKPOINT_PATH` | unset | use local files instead of downloading (set both) |
| `DEVICE` | `auto` | `auto`, `cpu`, `cuda`, `mps` |
| `TILE_SIZE`, `TILE_OVERLAP` | `512`, `64` | tiling of large rasters |
| `BATCH_SIZE` | `2` | tiles per forward pass |
| `MAX_PIXELS` | `16000000` | largest raster accepted |
| `MAX_BODY_MB` | `512` | largest request body |

## How the model is loaded

The published YAML configuration describes the architecture (Prithvi-EO-2.0 300M
encoder, UperNet decoder) and the normalisation statistics. The service builds the
`SemanticSegmentationTask` from that file with `backbone_pretrained=False` (so that
TerraTorch does not download the generic pre-training weights first) and then loads
the fine-tuned checkpoint **strictly**: a missing or unexpected tensor is an error, not
a warning. The commit hash of the downloaded weights is recorded as the model version
in every burned-area product.

## Tests

```bash
pip install --group dev .            # contract and tiling tests (no PyTorch needed; pip >= 25.1)
python -m pytest tests -q

pip install ".[prithvi]"             # adds the tests that build the real architecture
python -m pytest tests -q
```

The tests that need PyTorch build the full 300M-parameter architecture with random
weights and check tiling, normalisation, device handling and strict checkpoint
loading. They do **not** download the published checkpoint, so they say nothing about
the accuracy of the model.

## Limits to keep in mind

* The model was fine-tuned on burn scars in the **contiguous United States** (HLS Burn
  Scars dataset). Its accuracy on Mediterranean and Saharan-fringe vegetation is not
  established; in NarSat every model product is stored next to a dNBR product and the
  agreement between the two is reported, never assumed.
* The model sees one post-fire image. It has no notion of "before", so old scars and
  dark, sparsely vegetated surfaces can be confused with fresh burns. The pipeline
  keeps only patches close to the thermal detections of the event.
