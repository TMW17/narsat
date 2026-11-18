# 0002 - SeaweedFS as the S3-compatible object store

**Context.** Iceberg data files and the Cloud-Optimized GeoTIFFs of each event need an
object store with the S3 API, so that the same code runs locally and on a cloud bucket.
MinIO, the usual choice, archived its community-edition repository in April 2026.

**Decision.** SeaweedFS (Apache-2.0) with its S3 gateway, credentials from the
environment, anonymous access refused. All storage access goes through `pyarrow.fs` with
`file://` or `s3://` URIs, so the lake can also live on a local disk (tests, development)
or on any S3-compatible service without code changes.

**Consequences.** SeaweedFS allocates volumes per bucket; a development server needs a
sensible `-volume.max`. Its S3 compatibility is broad but not complete: NarSat only uses
basic object operations and multipart upload, both exercised by the integration tests.
