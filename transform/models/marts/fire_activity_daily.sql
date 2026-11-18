-- Daily fire activity per first-level administrative unit.
select
    activity_date,
    country_iso3,
    admin1_name,
    cast(count(*) as bigint) as n_hotspots,
    cast(count(distinct event_id) as bigint) as n_events,
    cast(sum(frp_mw) as double) as frp_sum_mw
from {{ ref('stg_hotspots') }}
group by activity_date, country_iso3, admin1_name
