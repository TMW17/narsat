# Roadmap

Version 0.1 is the platform: ingestion, events, two mapping methods, lakehouse, quality
gates, API, map, orchestration and monitoring. The next steps, in order:

## 1. First real run: done

The stack ran on the target machine on 17–18 November 2026; see
[validation-report.md](validation-report.md#6-first-real-run-1718-november-2026) and
[results.md](results.md).

## 2. Regional benchmark of the two methods: started

Done:

* 403 events of July–August 2023 mapped with both methods.
* Their disagreement explained pixel by pixel.
* Accuracy measured against NEALGEBA in north-east Algeria.

dNBR was the more accurate method there.

Next:

* Add references for Morocco and Tunisia (EFFIS, under its terms) and for more seasons.
* Report accuracy per vegetation type and fire size, with confidence intervals.
* Add a secondary evaluation restricted to the area each event's maps can cover, so
  that recall is no longer a lower bound.
* Investigate the 167 events where neither method found a burn.

## 3. Adapting the model to the region

* The benchmark shows a gap: the published model has a precision of 0.573 against 0.765
  for dNBR. Fine-tune it on regional examples with a parameter-efficient method (for
  example LoRA). NEALGEBA's maps of 1984–2022 are candidate labels, with 2023 held out
  as the test set, after checking that their dates and Landsat resolution suit
  Sentinel-2 training. Keep a held-out set of fires never used for training.
* Re-evaluate the gated model (model pixels kept where dNBR shows change), which reached
  a precision of 0.678 without any training.
* Compare against the original model and the dNBR baseline on the frozen evaluation set;
  version the weights and record the model version on every product (already supported).

## 4. Product features

* Burn severity classes from dNBR, alongside the burned/unburned mask.
* Notifications for new large events (e-mail or webhook), with rate limiting.
* Post-fire vegetation recovery over the following seasons.

## 5. Production deployment path

* Managed or clustered Kafka and object storage; an Iceberg REST catalog if other
  engines need the tables.
* HTTPS, single sign-on (OpenID Connect) instead of local accounts, secrets from a
  secret manager instead of `.env`.
* Several mapping workers. Today the queue assumes one worker (the hourly DAG allows one
  run at a time): `pending_events` does not lock the rows it returns, so two concurrent
  workers could map the same event. Claiming events with `FOR UPDATE SKIP LOCKED` and a
  lease would remove that limit.
* Alert notifications (Alertmanager) and log aggregation.
