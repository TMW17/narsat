"""The lakehouse layer.

* ``storage``  - object storage for rasters and JSON documents (local files or S3).
* ``catalog``  - the Apache Iceberg catalog and table definitions.
* ``bronze``   - exactly-once landing of raw hotspot messages.
* ``silver``   - validated, de-duplicated, enriched tables.
* ``gold``     - product tables (burned areas and their validation).
* ``maintenance`` - snapshot expiry and small-file compaction.
"""
