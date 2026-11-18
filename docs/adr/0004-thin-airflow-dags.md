# 0004 - Airflow DAGs only call the `narsat` command line

**Context.** Batch work (mapping, lake refresh, marts, maintenance, backfills) must be
scheduled and retried. Airflow pins hundreds of dependencies through its constraints
file; the platform pins its own (PyIceberg, PyArrow, rasterio...). Pipeline code written
inside DAG files is hard to test and to run by hand.

**Decision.** Airflow 3 runs in its own virtual environment, installed with the official
constraints; the platform and dbt are installed in another (`/opt/narsat`). Each task is
a `BashOperator` running one `narsat` or `dbt` command. Parameters of the manual backfill
are validated by Airflow (enumeration and ISO dates) before they reach the command line.

**Consequences.** Every task can be reproduced in a shell; the pipeline is tested
without Airflow. Airflow sees exit codes, not structured results: details are in the
command logs and in the database (quality results, mapping notes).
