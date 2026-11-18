-- Current fire events. The lake keeps every event ever created; one that was absorbed
-- by another ("merged") or emptied when its detections turned out to be a static heat
-- source ("dissolved") is history, not a fire.
select
    event_id,
    status,
    mapping_status,
    first_seen,
    last_seen,
    cast(year(first_seen) as integer) as fire_year,
    cast(n_hotspots as bigint) as n_hotspots,
    frp_max_mw,
    coalesce(country_iso3, '{{ var("unknown_country") }}') as country_iso3,
    coalesce(admin1_name, '{{ var("unknown_admin") }}') as admin1_name,
    hull_area_ha
from {{ source('silver', 'fire_events') }}
where status in ('active', 'cooling', 'closed')
