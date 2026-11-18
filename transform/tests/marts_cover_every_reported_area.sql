-- Reconciliation: the per-unit mart and the per-event mart are two views of the same
-- products, so their totals per method must agree (to rounding).
with by_unit as (
    select method, sum(area_ha) as area_ha
    from {{ ref('burned_area_admin_year') }}
    group by method
),

by_event as (
    select 'dnbr' as method, sum(area_dnbr_ha) as area_ha from {{ ref('event_summary') }}
    union all
    select 'model' as method, sum(area_model_ha) as area_ha from {{ ref('event_summary') }}
)

select
    by_unit.method,
    by_unit.area_ha as area_by_unit,
    by_event.area_ha as area_by_event
from by_unit
inner join by_event
    on by_unit.method = by_event.method
where abs(by_unit.area_ha - coalesce(by_event.area_ha, 0)) > 0.01
