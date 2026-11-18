# 0003 - Apache Iceberg with a SQL catalog in PostgreSQL

**Context.** The lake needs ACID appends from the stream, atomic partition replacement
for the nightly rebuild, snapshot history, and somewhere to store the Kafka offsets
atomically with the data. A catalog is required; REST catalog servers are one more
service to run and secure.

**Decision.** Apache Iceberg through PyIceberg with its **SQL catalog**: SQLite in tests
and development, a dedicated PostgreSQL database (`narsat_catalog`) in Compose. Tables
are partitioned by an explicit `acq_month` column (identity transform), because
PyIceberg's dynamic partition overwrite with non-identity transforms needs an optional
native extension.

**Consequences.** No separate catalog service; commits are compare-and-swap on the
catalog row, which the bronze writer relies on. Engines that only speak the REST catalog
(some versions of Trino or Spark setups) would need one added later. DuckDB reads the
tables through PyIceberg in the dbt plugin rather than through DuckDB's own Iceberg
extension, which would have to be downloaded at run time.
