# End-to-end browser test

`ui_smoke.cjs` checks, in a headless Chromium, what a person sees in the web application
when the whole platform runs: the map draws and receives vector tiles from the API, an
event opens with both burned-area products and their agreement, a Sentinel-2 layer is
rendered by TiTiler, statistics and data-quality pages show the expected figures, and an
analyst can sign in and review a product. It saves screenshots.

**The data is synthetic** (`seed.py`): invented detections and generated rasters of
uniform vegetation with a burned disc. The model product comes from the inference
service's weight-free spectral engine. Nothing produced here is a result about real fires.

## Procedure

1. Services: a test database (see `docs/development.md`), the API, TiTiler and the
   inference service with `NARSAT_INFERENCE_ENGINE=nbr`, and the built web application
   served with its nginx configuration (or `npm run preview` in `web/`).
2. Build the fixture (it **recreates** the schema of the test database):

   ```bash
   NARSAT_TEST_DB_DSN=... NARSAT_E2E_INFERENCE_URL=http://127.0.0.1:8090 \
     uv run python -m tests.e2e.seed
   ```

   Add `NARSAT_E2E_S3_ENDPOINT`, `NARSAT_E2E_S3_ACCESS_KEY` and `NARSAT_E2E_S3_SECRET_KEY`
   to put the lake on an S3 store with the Compose layout (`s3://narsat/...`), which is
   what the nginx tile allowlist expects; TiTiler then needs the matching `AWS_*`
   variables.
3. Run the browser test (Playwright for Node.js with a Chromium build installed):

   ```bash
   NARSAT_E2E_URL=http://127.0.0.1:8080 node tests/e2e/ui_smoke.cjs
   ```

Exit code 0 means every check passed; screenshots go to `$NARSAT_E2E_DIR/screenshots`
(default `/tmp/narsat-e2e`).
