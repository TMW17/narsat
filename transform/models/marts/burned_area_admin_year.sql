-- Burned area per year, administrative unit and mapping method. The year is the year
-- the fire was first detected; an event is attributed to the unit of its centroid.
select
    events.fire_year,
    events.country_iso3,
    events.admin1_name,
    areas.method,
    cast(count(distinct areas.event_id) as bigint) as n_events,
    cast(sum(areas.area_ha) as double) as area_ha
from {{ ref('stg_burned_areas') }} as areas
inner join {{ ref('stg_fire_events') }} as events
    on areas.event_id = events.event_id
group by events.fire_year, events.country_iso3, events.admin1_name, areas.method
