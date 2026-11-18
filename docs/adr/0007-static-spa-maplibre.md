# 0007 - Static React application with MapLibre, behind nginx

**Context.** The interface is a map with panels; all data comes from the API and the tile
servers. Server-side rendering (Next.js) would add a Node.js server to operate without a
need for it.

**Decision.** React + TypeScript built by Vite into static files, served by nginx, which
also proxies `/api` and `/titiler` so that the browser talks to one origin (no CORS).
MapLibre GL (BSD-3-Clause) draws the vector tiles; the base map is OpenFreeMap (no key)
with a plain fallback when it cannot be reached. Charts are small hand-written SVG
components rather than a charting library.

**Consequences.** One nginx container serves the application. MapLibre's web worker is
an ES module (`.mjs`) that nginx's stock MIME table does not map to JavaScript; the
configuration sets it explicitly (found by the end-to-end browser test).
