-- Summary of a burned-area mapping run, used for docs/results.md.
--
-- Read-only. Run it after `narsat reference agreement`, so that the comparison between
-- the two methods covers every mapped event:
--
--   docker compose exec api narsat reference agreement
--   docker compose exec -T postgis psql -U narsat -d narsat < analysis/mapping_summary.sql

\echo
\echo '1. Mapping status of the fire events, by year and country'
SELECT extract(year FROM first_seen)::int AS year,
       coalesce(country_iso3, '-') AS country,
       count(*) FILTER (WHERE mapping_status = 'mapped') AS mapped,
       count(*) FILTER (WHERE mapping_status = 'imagery_pending') AS waiting_imagery,
       count(*) FILTER (WHERE mapping_status = 'failed') AS failed,
       count(*) FILTER (WHERE mapping_status = 'pending') AS still_queued,
       count(*) FILTER (WHERE mapping_status = 'skipped') AS skipped,
       count(*) FILTER (WHERE mapping_status = 'not_ready') AS not_ready
FROM fire_event
WHERE status IN ('active', 'cooling', 'closed')
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '2. Why events are not mapped, or mapped with a note'
SELECT mapping_status,
       left(regexp_replace(coalesce(mapping_note, '-'), '[0-9]+%', 'N%', 'g'), 90) AS note,
       count(*) AS events
FROM fire_event
WHERE status IN ('active', 'cooling', 'closed')
  AND mapping_status NOT IN ('skipped', 'not_ready')
  AND (mapping_status <> 'mapped' OR mapping_note IS NOT NULL)
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 15;

\echo
\echo '3. Products by method'
SELECT method,
       count(*) AS products,
       count(*) FILTER (WHERE area_ha > 0) AS found_burn,
       round(sum(area_ha)::numeric) AS total_ha,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY area_ha)
              FILTER (WHERE area_ha > 0))::numeric, 1) AS median_ha_if_found,
       round(max(area_ha)::numeric) AS max_ha
FROM burned_area
GROUP BY method
ORDER BY method;

\echo
\echo '4. Per event: which method found a burn'
SELECT CASE WHEN d.area_ha > 0 AND m.area_ha > 0 THEN 'both found burn'
            WHEN d.area_ha > 0 THEN 'only dNBR found burn'
            WHEN m.area_ha > 0 THEN 'only model found burn'
            ELSE 'neither found burn' END AS outcome,
       count(*) AS events,
       round(sum(d.area_ha)::numeric) AS dnbr_ha,
       round(sum(m.area_ha)::numeric) AS model_ha,
       round(sum(v.intersection_ha)::numeric) AS overlap_ha,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY v.iou))::numeric, 3) AS median_iou
FROM burned_area d
JOIN burned_area m ON m.event_id = d.event_id AND m.method = 'model'
LEFT JOIN burned_area_validation v ON v.ba_id = m.ba_id AND v.ref_source = 'narsat:dnbr'
WHERE d.method = 'dnbr'
GROUP BY 1
ORDER BY 2 DESC;

\echo
\echo '5. Agreement between the methods, pooled over all events'
SELECT count(*) AS pairs,
       round(sum(intersection_ha)::numeric) AS overlap_ha,
       round((sum(intersection_ha) / NULLIF(sum(union_ha), 0))::numeric, 3) AS pooled_iou,
       round((sum(intersection_ha) / NULLIF(sum(ref_ha), 0))::numeric, 3) AS dnbr_share_in_model,
       round((sum(intersection_ha) / NULLIF(sum(mapped_ha), 0))::numeric, 3) AS model_share_in_dnbr
FROM burned_area_validation
WHERE ref_source = 'narsat:dnbr';

\echo
\echo '6. The 15 largest fires by dNBR area'
SELECT left(e.event_id::text, 8) AS event,
       e.country_iso3 AS cc,
       left(a.name, 18) AS region,
       e.first_seen::date AS first_day,
       e.n_hotspots AS det,
       round(d.area_ha::numeric) AS dnbr_ha,
       round(m.area_ha::numeric) AS model_ha,
       round(v.iou::numeric, 2) AS iou
FROM fire_event e
JOIN burned_area d ON d.event_id = e.event_id AND d.method = 'dnbr'
LEFT JOIN burned_area m ON m.event_id = e.event_id AND m.method = 'model'
LEFT JOIN burned_area_validation v ON v.ba_id = m.ba_id AND v.ref_source = 'narsat:dnbr'
LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id
ORDER BY d.area_ha DESC
LIMIT 15;
