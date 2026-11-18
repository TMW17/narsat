-- Fire detections that count as fire activity: persistent static sources (gas flares,
-- industrial sites) are excluded here once, for every mart.
select
    hotspot_id,
    event_id,
    acq_ts,
    cast(acq_ts as date) as activity_date,  -- UTC day (the session time zone is UTC)
    coalesce(country_iso3, '{{ var("unknown_country") }}') as country_iso3,
    coalesce(admin1_name, '{{ var("unknown_admin") }}') as admin1_name,
    frp_mw,
    confidence_level
from {{ source('silver', 'hotspots') }}
where not is_static_source
