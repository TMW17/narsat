# Architecture decision records

Short records of the decisions that shape NarSat: the context, the decision, and what it
costs. A decision is changed by adding a new record that supersedes the old one.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-kafka-kraft.md) | Apache Kafka in KRaft mode for the detection stream | Accepted |
| [0002](0002-seaweedfs-object-storage.md) | SeaweedFS as the S3-compatible object store | Accepted |
| [0003](0003-iceberg-sql-catalog.md) | Apache Iceberg with a SQL catalog in PostgreSQL | Accepted |
| [0004](0004-thin-airflow-dags.md) | Airflow DAGs only call the `narsat` command line | Accepted |
| [0005](0005-separate-inference-service.md) | The model runs in a separate inference service | Accepted |
| [0006](0006-two-methods-compared.md) | Two burned-area methods, always compared | Accepted |
| [0007](0007-static-spa-maplibre.md) | Static React application with MapLibre, behind nginx | Accepted |
| [0008](0008-tile-allowlist.md) | Raster tiles only through an allowlist in nginx | Accepted |
