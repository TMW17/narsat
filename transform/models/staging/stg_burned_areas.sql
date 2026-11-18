-- Burned-area products that may be reported: anything an analyst rejected is left out.
select
    ba_id,
    event_id,
    method,
    model_version,
    post_datetime,
    area_ha,
    qa_status
from {{ source('gold', 'burned_areas') }}
where qa_status <> 'rejected'
