# Results

These results come from running NarSat on live public data. No numbers are simulated.

- **Machine:** a MacBook Pro (Apple Silicon, 16 GB, macOS 26).
- **Containers:** the services ran in Docker under Colima (4 CPUs, 8 GB).
- **Model service:** run natively on the Apple GPU (MPS).
- **Code:** the version that introduced `narsat map compare` (2026-11-17).

Each section gives its data window and settings. This page grows as more of the
platform is run; anything not measured yet is listed under "Not measured yet".

## 1. Summer 2023 backfill: fire detections and events

| Setting | Value |
| --- | --- |
| Source | NASA FIRMS, VIIRS S-NPP standard processing (`VIIRS_SNPP_SP`) |
| Window | 2023-07-01 to 2023-08-31 |
| Area | bounding box 13.5° W to 11.8° E, 27.0° N to 37.6° N (Morocco, northern Algeria, Tunisia). The box also takes in parts of southern Spain and Portugal; events there are kept but never mapped |
| Boundaries | geoBoundaries ADM1 (ODbL): Algeria 48 wilayas (2017 set), Morocco 12 regions, Tunisia 24 governorates |

| Measure | Value |
| --- | --- |
| Detections stored | 20,784 |
| Flagged as static sources (gas flares, industry) | 16,305 (78 %) |
| Events queued for burned-area mapping | 403: Algeria 288, Morocco 63, Tunisia 52 |
| Single-detection events, not mapped | 576 |

The largest 2023 events by detections were, in order:

1. Bouira (20–25 July)
2. Oriental (Morocco)
3. Bejaia
4. Bouira (24–25 July)
5. Souss-Massa
6. Médéa
7. Skikda
8. Jendouba
9. El Tarf
10. Fès-Meknès

## 2. One fire mapped with both methods: Bouira, Algeria, 20–25 July 2023

Event `92ef5d48-4477-5a41-bdf4-7f22ea40d786`, 241 VIIRS detections, mapped on 2026-11-17.

| Setting | Value |
| --- | --- |
| Images | Sentinel-2 L2A from Earth Search (`sentinel-2-c1-l2a`), tile 31SEA, relative orbit 108. Pre-fire: Sentinel-2A, 2023-07-18. Post-fire: Sentinel-2B, 2023-08-02 |
| dNBR | Otsu threshold, clamped to 0.10–0.44; it came out at 0.303. Pixels with pre-fire NDVI below 0.15 are not analysed |
| Model | Prithvi-EO-2.0-300M-BurnScars, version `a3f2c410e45b`, run at 30 m with a probability threshold of 0.5 |
| Both methods | Patches under 1 ha, or farther than 750 m from every detection, are removed |

### Area and support from the fire detections

| | dNBR | Model |
| --- | --- | --- |
| Burned area | 4,494.6 ha | 7,328.4 ha |
| Polygons | 185 | 3 |
| Clear sky over the area | 95.6 % | 100 % |
| Mean score inside the burned area | dNBR 0.566 | probability 0.952 |
| Detections within 375 m (one VIIRS pixel) | 232 of 241 | 239 of 241 |
| Burned area more than 2 km from any detection | 153.3 ha | 239.1 ha |

**Agreement between the two maps:**

- 4,489.2 ha are burned in both.
- IoU is 0.612.
- 99.9 % of the dNBR area lies inside the model area.
- 61.3 % of the model area lies inside the dNBR area.

**Reproducibility and speed:**

- Mapping the event a second time gave the identical dNBR product: 4,494.63 ha and 185
  polygons.
- The whole mapping of the event took 30.8 s, most of it reading the images.
- The segmentation request took about 1.4 s, timed from the model-information call to the
  answer.

### Where the methods disagree

`narsat map compare` puts every pixel that only the model calls burned into one class.
Areas are 20 m pixel counts, so they differ slightly from the polygon areas.

| Class | Area | Share | Median dNBR | Median NDVI before | Median NDVI after |
| --- | --- | --- | --- | --- | --- |
| Bare, sparse or water before the fire (not analysed by dNBR) | 283.2 ha | 10.0 % | – | 0.092 | 0.082 |
| No clear view | 0.3 ha | 0.0 % | – | 0.161 | 0.129 |
| Unchanged (dNBR below 0.10) | 877.2 ha | 30.9 % | 0.040 | 0.312 | 0.296 |
| Low severity (dNBR 0.10–0.27) | 1,315.0 ha | 46.4 % | 0.196 | 0.322 | 0.188 |
| dNBR 0.27 to the 0.303 threshold | 300.2 ha | 10.6 % | 0.286 | 0.374 | 0.179 |
| Above the threshold, removed as small or distant patches | 60.9 ha | 2.1 % | 0.353 | 0.472 | 0.185 |
| **All model-only pixels** | **2,836.9 ha** | | | | |

Only 5.2 ha are burned in the dNBR map and not in the model map. In all of them the
model saw the ground but scored it below 0.5.

**Reading:**

- **About 41 % of the model's extra area shows no burn signal.** That is 1,160 ha: ground
  that was bare before the fire, or vegetation whose NBR and NDVI barely changed. Judging
  by the screenshots, much of the bare ground is the exposed shore of the reservoir
  inside the fire. Most of it is probably a
  model error.
- **About 59 % shows a real but weak burn signal.** That is 1,676 ha, with NDVI dropping
  by 0.13 to 0.29 between the two images. dNBR left it out because its automatic
  threshold (0.303) sits just above the USGS boundary between low and moderate-low
  severity. So on this fire, the dNBR map is a map of moderate and higher severity.
- **A visual check had suggested something else.** On the SWIR false-colour image, the
  gaps inside the dNBR map look green, which suggested unburned islands. The measurement
  shows that most of them changed. A low-severity fire that leaves the canopy green looks
  like this in that rendering.

**Limits:** this is one fire. The USGS severity classes (Key and Benson, 2006) were
calibrated on North American forests. No reference perimeters have been loaded yet, so
neither map has been checked against an independent outline.

## 3. All queued 2023 fires, mapped with both methods

The run was one `narsat map pending --limit 500` in a background container, from
2026-11-17 23:05 UTC to 2026-11-18 02:17 UTC, so 3 h 12 min. The settings were those of
section 2, with the dNBR threshold chosen per event. The tables come from
`analysis/mapping_summary.sql`.

| Measure | Value |
| --- | --- |
| Events attempted | 403 |
| Mapped with both methods | 402 |
| Crashed, to be retried on the next run | 1 (a failed image read) |
| Events with no usable image | 0 |
| Mean time per event | 28.5 s |

| Products (402 events) | dNBR | Model |
| --- | --- | --- |
| Events where it found a burn | 211 | 110 |
| Total burned area | 43,743 ha | 66,107 ha |
| Median area, when a burn was found | 24.4 ha | 197.8 ha |
| Largest | 4,866 ha | 7,328 ha |

| Per event | Events | dNBR area | Model area | Overlap | Median IoU |
| --- | --- | --- | --- | --- | --- |
| Neither found a burn | 167 | 0 | 0 | 0 | – |
| Only dNBR found a burn | 125 | 2,591 ha | 0 | 0 | 0 |
| Both found a burn | 86 | 41,152 ha | 64,088 ha | 39,927 ha | 0.596 |
| Only the model found a burn | 24 | 0 | 2,019 ha | 0 | 0 |

Over all 402 events together:

- IoU is 0.571.
- 91.3 % of the dNBR area lies inside the model area.
- 60.4 % of the model area lies inside the dNBR area.

The 15 largest fires by dNBR area:

| Region | Country | First detection | Detections | dNBR | Model | IoU |
| --- | --- | --- | --- | --- | --- | --- |
| Bejaia | DZA | 2023-07-23 | 165 | 4,866 ha | 6,609 ha | 0.73 |
| Bouira | DZA | 2023-07-20 | 241 | 4,495 ha | 7,328 ha | 0.61 |
| Oriental | MAR | 2023-07-16 | 174 | 2,325 ha | 3,200 ha | 0.72 |
| Skikda | DZA | 2023-07-24 | 74 | 1,842 ha | 2,599 ha | 0.71 |
| El Tarf | DZA | 2023-08-27 | 68 | 1,840 ha | 2,299 ha | 0.80 |
| Bouira | DZA | 2023-07-24 | 149 | 1,804 ha | 2,996 ha | 0.60 |
| Bejaia | DZA | 2023-07-23 | 41 | 1,659 ha | 2,793 ha | 0.59 |
| Bejaia | DZA | 2023-07-24 | 11 | 1,617 ha | 1,875 ha | 0.86 |
| Bejaia | DZA | 2023-07-24 | 33 | 1,388 ha | 1,836 ha | 0.76 |
| Boumerdès | DZA | 2023-07-23 | 20 | 1,314 ha | 2,114 ha | 0.62 |
| Médéa | DZA | 2023-07-17 | 91 | 1,302 ha | 2,396 ha | 0.54 |
| Bejaia | DZA | 2023-07-24 | 63 | 1,280 ha | 2,273 ha | 0.56 |
| Siliana | TUN | 2023-07-25 | 39 | 1,074 ha | 1,409 ha | 0.76 |
| Jendouba | TUN | 2023-07-24 | 69 | 1,060 ha | 1,910 ha | 0.55 |
| Bejaia | DZA | 2023-07-24 | 32 | 1,019 ha | 1,673 ha | 0.60 |

**Reading:**

- **On the large fires, the Bouira pattern holds.** Where both methods found a burn
  (86 events), the model's map contains 97 % of the dNBR area (39,927 of 41,152 ha) and
  is 56 % larger.
- **The model misses small fires.** In 125 events (31 %), only dNBR found burned ground:
  2,591 ha in all, about 21 ha per event. When the model finds a burn, its median is
  198 ha, against 24 ha for dNBR.
  - Two causes are plausible but have not been tested: the model runs at 30 m instead of
    20 m, and the scars it was trained on are large.
- **Not examined yet:**
  - The 24 events where only the model found a burn (2,019 ha).
  - The 167 events (42 %) where neither method found a burned patch of at least 1 ha
    within 750 m of the detections. These numbers alone cannot say why.
- **Fire events vs. fires:** the six Bejaia rows are separate events because the
  detections were clustered into separate events. They may be parts of the same fire
  complex.

The event that crashed was mapped on the next run (2026-11-18 04:29 UTC): dNBR found
3.3 ha, the model nothing. That moves it to "only dNBR found a burn", which now has 126
events. All 403 queued events are mapped.

## 4. Why the methods disagree, over all fires

`narsat map compare --all` ran on 2026-11-18. It compared the 236 events where at least
one method found a burn, in 8 s, and skipped none. Areas are 20 m pixel counts.

| Model-only area, by class | Both found a burn (86 events) | Only the model (24 events) | All 236 events |
| --- | --- | --- | --- |
| Bare, sparse or water before the fire | 3,220.1 ha (13.3 %) | 1,567.4 ha (77.6 %) | 4,787.5 ha (18.3 %) |
| No clear view | 251.6 ha (1.0 %) | 0.3 ha (0.0 %) | 251.9 ha (1.0 %) |
| Unchanged (dNBR below 0.10) | 11,437.0 ha (47.4 %) | 440.3 ha (21.8 %) | 11,877.3 ha (45.4 %) |
| Low severity (dNBR 0.10–0.27) | 6,407.1 ha (26.5 %) | 9.9 ha (0.5 %) | 6,417.0 ha (24.5 %) |
| dNBR 0.27 up to the event's threshold | 2,274.8 ha (9.4 %) | 0.0 ha | 2,274.8 ha (8.7 %) |
| Above the threshold, removed as small or distant patches | 552.6 ha (2.3 %) | 1.9 ha (0.1 %) | 554.5 ha (2.1 %) |
| **Total model-only area** | **24,143.2 ha** | **2,019.8 ha** | **26,163.0 ha** |

**Burned in dNBR but not in the model:**

- 1,218.4 ha in the 86 events where both found a burn.
- 2,594.1 ha in the 126 events where only dNBR found one.
- In every case the model had a clear view of the ground and scored it below 0.5.

**Reading:**

- **Over all fires, about two thirds of the model's extra area (63.7 %, 16,665 ha) shows
  no burn signal.** It was bare before the fire or did not change.
- **About a third (35.3 %, 9,246 ha) shows a weak burn signal** that dNBR's threshold
  left out. That area equals 21 % of the dNBR total.
- **Bouira is not typical.** There, the split was the other way round: 41 % no signal,
  59 % weak signal.
- **The events found only by the model are almost all false positives.** In those 24
  events, 99.4 % of the area is ground that was bare before the fire (77.6 %) or did not
  change (21.8 %).
- **The model sees the small fires but does not call them burned.** On the 126
  dNBR-only events, it had a view of every burned dNBR pixel and scored them all below
  0.5.
- **What this suggests:** gating the model by the pre-fire vegetation and by a minimum
  change would remove most of its errors. Reporting dNBR severity classes would show the
  low-severity burn that the binary map leaves out. Neither change has been made or
  tested. Both should be judged against an independent reference, not against these
  same fires.

## 5. Accuracy against an independent reference: NEALGEBA

The reference is NEALGEBA v1.0, the 2023 layer. It is a Landsat burned-area product of
north-east Algeria by Kouachi et al. (2024), CC BY 4.0. It is loaded by place only,
because its dates lag the fires.

These choices were fixed on 2026-11-18, before the reference was loaded or looked at:

| Choice | Value |
| --- | --- |
| Maps evaluated | dNBR as mapped; the model as mapped; the model kept only where dNBR could analyse the ground and is at least 0.10; the union of dNBR and that gated model |
| Zone | The six wilayas NEALGEBA covers (Annaba, Béjaïa, El Tarf, Jijel, Skikda, Tizi Ouzou) |
| Events | All mapped events in that zone |
| Pixels | The 20 m event grids; a pixel shared by overlapping events counted once |
| Measures | Precision, recall, IoU and Dice by area; recall is limited to the reference burned area inside the event grids |

### Result

The evaluation ran on 2026-11-18 (`narsat map evaluate`). It covered 63 mapped events in
the six wilayas, against all 2,727 polygons of the 2023 layer.

| Map | Mapped | True positive | False positive | False negative | Precision | Recall | IoU | Dice |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **dNBR** | 20,808 ha | 15,927 ha | 4,881 ha | 3,472 ha | **0.765** | 0.821 | **0.656** | **0.792** |
| Model | 28,319 ha | 16,234 ha | 12,085 ha | 3,165 ha | 0.573 | 0.837 | 0.516 | 0.680 |
| Gated model | 23,732 ha | 16,095 ha | 7,637 ha | 3,304 ha | 0.678 | 0.830 | 0.595 | 0.746 |
| dNBR or gated model | 25,024 ha | 16,516 ha | 8,508 ha | 2,883 ha | 0.660 | **0.851** | 0.592 | 0.744 |

NEALGEBA maps 19,399 ha burned inside the event grids.

Per event, on the 15 events with the most reference burned area:

| Event | Wilaya | First detection | Reference | IoU dNBR | IoU model | IoU gated | IoU union |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `a655531c` | Bejaia | 2023-07-23 | 4,993 ha | **0.719** | 0.600 | 0.641 | 0.641 |
| `64ba909d` | Bejaia | 2023-07-24 | 1,818 ha | **0.823** | 0.763 | 0.804 | 0.804 |
| `ea204be3` | El Tarf | 2023-08-27 | 1,760 ha | **0.894** | 0.739 | 0.839 | 0.839 |
| `186c2350` | Skikda | 2023-07-24 | 1,647 ha | **0.786** | 0.609 | 0.676 | 0.676 |
| `ea774542` | Bejaia | 2023-07-24 | 1,644 ha | **0.737** | 0.660 | 0.709 | 0.709 |
| `423905cf` | Bejaia | 2023-07-24 | 1,445 ha | **0.485** | 0.365 | 0.439 | 0.438 |
| `0892a556` | Bejaia | 2023-07-23 | 1,217 ha | **0.476** | 0.316 | 0.390 | 0.388 |
| `0fe35b45` | Bejaia | 2023-07-24 | 929 ha | **0.474** | 0.335 | 0.425 | 0.422 |
| `39c93d7a` | Bejaia | 2023-07-24 | 902 ha | **0.812** | 0.767 | 0.803 | 0.803 |
| `439815ac` | Bejaia | 2023-07-16 | 544 ha | 0.018 | 0.000 | 0.000 | 0.018 |
| `5ebef4a6` | Bejaia | 2023-07-22 | 530 ha | 0.002 | 0.000 | 0.000 | 0.002 |
| `1e545658` | Bejaia | 2023-08-27 | 524 ha | **0.746** | 0.350 | 0.582 | 0.582 |
| `e6978622` | Jijel | 2023-07-24 | 500 ha | **0.216** | 0.157 | 0.158 | 0.208 |
| `447fed2d` | El Tarf | 2023-07-25 | 495 ha | **0.688** | 0.664 | 0.652 | 0.652 |
| `bcf93f25` | El Tarf | 2023-08-26 | 471 ha | **0.875** | 0.527 | 0.792 | 0.792 |

**Reading:**

- **dNBR is the most accurate map against NEALGEBA.** It has the best precision, IoU and
  Dice overall, and the best or equal-best IoU on each of the 15 largest events.
- **The model adds little and costs a lot.** Compared with dNBR, its recall is only 1.6
  points higher (306 ha more found), for 7,204 ha more false positives.
- **Gating works, but not enough.** The gate removed 4,587 ha of the model's area: 4,448 ha
  of false positives and only 139 ha of true positives. Precision rose from 0.573 to
  0.678, but the gated model still trails dNBR.
- **NEALGEBA does not confirm most of the weak-signal area.** Adding the gated model to
  dNBR adds 4,215 ha. Only 588 ha of it are burned in NEALGEBA, so the weak-signal
  pixels (dNBR from 0.10 up to the event's threshold) are mostly not burned according
  to this reference.
- **This corrects section 4.** There, I read that area as light burn that dNBR missed;
  this reference says otherwise. NEALGEBA could also miss light burn, so the question is
  not closed, but the evidence available now supports dNBR's automatic threshold.
- **Per-event values understate agreement in fire complexes.** The low values for some
  Bejaia events come from events that overlap. Each event's grid holds burns mapped by a
  neighbouring event, which the totals above count once. Very low values (`439815ac`,
  `5ebef4a6`) mean NEALGEBA mapped burns in that grid that are not in the event's
  images. They may be burns that happened later in 2023.

**Limits of this comparison:**

- NEALGEBA is another satellite product: Landsat, 30 m. Its authors report commission
  and omission errors below 8.2 % against Sentinel-2 references.
- It is annual, and matched by place only. Burns from other months of 2023 inside the
  event grids count as missed, which lowers recall.
- Edge pixels between 20 m and 30 m maps lower the IoU of small and fragmented burns.
- Recall covers only the fires NarSat detected (inside the event grids). It is also a
  lower bound, because the grid rectangles include corners that the maps never cover
  (see below).

The web application's event panel also shows a per-event agreement with NEALGEBA
(`narsat reference agreement`). The first version matched every NEALGEBA polygon within
2 km of an event. In fire complexes, that took in the neighbouring fires' burns, which
then counted as missed; a small Bejaia fire showed a recall of 0.04 for that reason.
Since 2026-11-18, the per-event comparison uses two rules:

1. **Matching:** a polygon belongs to an event when it lies within 750 m of one of the
   event's own detections. That is the same rule that decides which burned patches
   belong to the event in NarSat's maps. With it, 116 products have NEALGEBA polygons
   (124 with the 2 km rule).
2. **Clipping:** the matched reference is cut to the area the mapper examined (the
   event outline plus 1.5 km). This was added after the 750 m rule alone still gave the
   largest Bejaia event (`a655531c`) a recall of 0.36 and an IoU of 0.33. The reason is
   that one NEALGEBA polygon can cover a whole fire complex, so most of it lay far
   outside the event's images.

The pixel-level evaluation above is not affected; it already counted only the pixels
inside the event grids.

With both rules, the panel of `a655531c` shows these values against NEALGEBA:

| Map | IoU | Precision | Recall |
| --- | --- | --- | --- |
| dNBR | 0.81 | 0.84 | 0.95 |
| Model | 0.65 | 0.65 | 1.00 |

The precision values are unchanged by the clipping. The values are higher than this
event's pixel-level IoU (0.72 for dNBR), because the two comparisons cover different
areas:

- **The pixel evaluation counts the whole grid rectangle.** That includes its corners
  outside the examined area, where NarSat never maps anything, so NEALGEBA burn there
  counts as missed.
- **The panel's comparison counts only the examined area.**

The pixel evaluation was kept as it was registered. Its recall is therefore a lower
bound.

## 6. Lakehouse, quality gates, dbt and orchestration

All of these ran on the same Mac on 2026-11-18.

| Step | Result |
| --- | --- |
| `narsat lake refresh --all` | Rebuilt three months (2023-07, 2023-08, 2026-11): `silver.hotspots` 22,252 rows, `silver.fire_events` 1,456, `gold.burned_areas` 806, `gold.burned_area_validation` 527 (403 comparisons between the methods, 124 against NEALGEBA) |
| Quality gates | No blocking failure. One warning: `gold.burned_areas.non_empty_products`, where 322 of 806 products (40 %) contain burned area, below the 50 % expected when the check was written. It reflects the many detected fires that leave no mapped scar of at least 1 ha (sections 3 and 4) and is kept as a warning |
| Airflow 3 | Four DAGs loaded and active; `narsat_lakehouse_refresh` triggered by hand |
| `refresh_lake` task | Success in 1.7 s (months touched in the last 45 days) |
| `build_marts` task, `dbt build` with DuckDB | Success in 3.6 s: 3 external (Parquet) marts, 4 staging views, 43 data tests. `PASS=50 WARN=0 ERROR=0 SKIP=0 TOTAL=50` |
| `publish_marts` task | Success in 0.6 s |

## Not measured yet

- Agreement with reference perimeters outside north-east Algeria (for example EFFIS).
- The fire events of the live feed (November 2026), which are not yet queued for mapping.
