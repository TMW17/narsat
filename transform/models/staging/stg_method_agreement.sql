-- Agreement between the two mapping methods of one event: the foundation-model product
-- compared with the spectral-index (dNBR) product.
select
    event_id,
    iou,
    precision_ref,
    recall_ref
from {{ source('gold', 'burned_area_validation') }}
where method = 'model'
  and ref_source = 'narsat:dnbr'
