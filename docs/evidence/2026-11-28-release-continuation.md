# November 28 release validation

**Production verdict: NOT READY.**

Production Kafka consumers now disable implicit offset reset after explicit
assignment and leave the group if a reset error arrives during retry polling.
This closes a retention race during fetch. The original historical incident
trigger remains unproven. [Kafka reliability](../serving.md#reliability)

The full Windows unit rerun passed 745 tests with two environment-dependent
skips. All 54 PostgreSQL, Kafka and Redis integration tests passed. Strict
mypy passed on 121 files with the declared CI dependencies. Ruff, Bandit,
Compose rendering and Kustomize rendering passed. The local tracked-file
secret scan found zero matches, and CI now runs the same check.

The Linux serving image built successfully and passed an isolated champion
load and Kafka client configuration smoke test under UID 10001. Scoped
disposition of unfiltered image findings, matched sustained capacity, live
alert and recovery verification, and public end-to-end validation are open.
[Release checklist](../production-readiness.md)

The rebuilt proxy ran as UID and GID 10001 with zero effective capabilities
and no-new-privileges. Local HTTP, verified TLS and certificate persistence
across restart passed. Its unfiltered image scan had zero High or Critical
findings. The CI-aligned Windows unit suite passed 728 tests with four
environment-dependent skips. The earlier capacity image set used a different
proxy digest and remains historical for this proxy change.

The requirements matrix records the supporting model, feature-store, dashboard
and MLOps evidence and the unresolved React and Streamlit specification question.
[Requirements matrix](../aiml-01-completion.md)
