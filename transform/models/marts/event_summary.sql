-- One row per fire event with its mapped area by method and the agreement between methods.
with areas as (
    select
        event_id,
        max(case when method = 'dnbr' then area_ha end) as area_dnbr_ha,
        max(case when method = 'model' then area_ha end) as area_model_ha
    from {{ ref('stg_burned_areas') }}
    group by event_id
)

select
    events.event_id,
    events.country_iso3,
    events.admin1_name,
    events.first_seen,
    events.last_seen,
    cast(date_diff('second', events.first_seen, events.last_seen) / 3600.0 as double)
        as duration_hours,
    events.n_hotspots,
    events.frp_max_mw,
    areas.area_dnbr_ha,
    areas.area_model_ha,
    agreement.iou as method_iou
from {{ ref('stg_fire_events') }} as events
left join areas
    on events.event_id = areas.event_id
left join {{ ref('stg_method_agreement') }} as agreement
    on events.event_id = agreement.event_id
