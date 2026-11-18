# 0001 - Apache Kafka in KRaft mode for the detection stream

**Context.** Detections arrive continuously and feed two sinks (lake, PostGIS) and an
event feed. A durable log decouples the poller from the consumers, allows replay, and
gives the dead-letter pattern a home. The initial design used Redpanda, which is
distributed under the Business Source License, not an open-source licence.

**Decision.** Apache Kafka 4 (Apache-2.0), single node in KRaft mode (no ZooKeeper),
`confluent-kafka` (librdkafka) clients. Topics are created explicitly with their
settings; automatic topic creation is disabled. The producer is idempotent with
`acks=all`; the consumer commits offsets manually after both sinks are durable.

**Consequences.** One JVM container (about 0.5 GB of heap). The volume rate (thousands of
messages per day in a fire season) would not need Kafka on its own; it is used because
replay, consumer groups and exactly-once landing are part of what the project
demonstrates. Tests use librdkafka's in-process mock cluster, which does not implement
every admin request; behaviour against a real broker is covered only by the Compose
stack.
