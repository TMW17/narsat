# 0008 - Raster tiles only through an allowlist in nginx

**Context.** TiTiler renders whatever raster its `url` parameter names: a local file, an
object in any bucket it has credentials for, or an internal HTTP address. Exposed as is,
it is a server-side request forgery and data-exposure risk.

**Decision.** TiTiler is not published to the network except on 127.0.0.1 for
development. nginx forwards `/titiler/cog/tiles/...` only when the **whole query string**
matches what the API generates: a `.tif` under `s3://narsat/assets/`, one to three band
indices, a numeric range, and one of three colour maps. Every other TiTiler endpoint
returns 404. TiTiler's STAC, mosaic and multidimensional endpoints are disabled.

**Consequences.** New renderings require changing the API and the pattern together.
Matching the whole query string, not only `url`, matters: a repeated or
percent-encoded parameter name would otherwise smuggle a second URL past the check
(both cases are refused in testing).
