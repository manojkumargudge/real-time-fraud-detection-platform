# Private sustained capacity and security rehearsal

The approved private rehearsal reuses `fraud-release-capacity-1126` in `fraud-detection-platform-2026/us-central1-a`, instance ID `938503370005181237`. The VPC is `fraud-rehearsal-vpc`, subnet `fraud-rehearsal-subnet`, and the only applicable ingress rule and target tag are `fraud-release-rehearsal-iap` (TCP 22 from the IAP range). The retained boot disk is `fraud-release-capacity-1126` (100 GiB balanced, auto-delete disabled). Leave `fraud-release-rehearsal` and its separate 100 GiB disk stopped. Neither host has a reserved IP. The eight-vCPU VM uses an ephemeral IPv4 address for outbound traffic. The legacy startup script was removed before the telemetry pilot.

## Cost and runtime control

At the Iowa planning rates of US$0.26805/hour for compute and US$0.005/hour for in-use IPv4, three hours costs **US$0.82 (₹76)** and four hours costs **US$1.09 (₹101)**, incremental to disk retention. Allow US$0.20 (₹19) for a maximum 1 GiB evidence export in this estimate and US$0.20 for variable telemetry/transfer. Request **US$2 (₹185) before tax** for a no-publication run. This is an approval envelope. The existing disks continue billing at about US$20/month (₹1,850) combined, or US$0.11 total over four hours. No new disk, snapshot, NAT, static IP, DNS or Artifact Registry is needed for the cached-manifest rehearsal. INR values use the RBI ₹92.48/USD reference for planning. Rates and any tax are governed by the billing account. [Compute pricing](https://cloud.google.com/products/compute/pricing/general-purpose) [Disk pricing](https://cloud.google.com/compute/disks-image-pricing) [Network pricing](https://cloud.google.com/vpc/network-pricing) [RBI reference](https://www.rbi.org.in/?id=10598)

An independently approved private registry would add US$0.10/GiB-month above the account's 0.5 GiB allowance (10 GiB is about US$0.95–1.00/month). Same-region pulls to the VM cost US$0 for transfer. Retention and any external downloads accrue separately. The project is billing-linked and has the project rehearsal budget `Fraud private rehearsal INR 450` with 50%, 80%, and 100% gross-spend alerts and a 100% forecast alert. Budgets notify. The platform STOP policy bounds compute runtime. The billing owner must confirm Free Trial status, remaining applicable credit and expiry in Billing Overview. Active trial credit would cover eligible charges if sufficient, and a billing link alone cannot establish coverage. [Registry pricing](https://cloud.google.com/artifact-registry/pricing) [Trial terms](https://docs.cloud.google.com/free/docs/free-cloud-features)

The billing owner confirmed an active Free Trial with ₹28,436 in Welcome
credit and expiry February 8, 2027. The authorization is cumulative and capped
at US$2 in eligible Welcome credits. The estimate before `1127e`, including
prior failed starts, was US$0.23 for compute and ephemeral IPv4. One further
four-hour window plus the US$0.40 variable allowance brings the estimated
cumulative incremental total to US$1.72 (₹159), with existing disk retention
tracked separately. The estimate is based on audited runtime and rates.

For the `1127f` continuation, conservatively including initial inventory host
creation and the interrupted runs through `1127e`, completed Compute Engine
operation intervals account for about 142 minutes, or US$0.65 for compute and
IPv4. A further four hours costs up to US$1.09 for those two items. A US$0.05
transfer allowance and a US$0.20 telemetry allowance bring the cumulative
planning upper bound to about US$1.99 (₹184) before existing disk retention.
The small evidence archives and loopback pulls support the reduced transfer
allowance. A 180-minute guest poweroff timer adds to the 210-minute operator
guard and existing four-hour Compute Engine STOP limit. The resumed workload
checks the prior archive and imports only a complete, matching `main-1` run.

After approval, run these commands from Linux with gcloud, OS Login and IAP rights:

```bash
P=fraud-detection-platform-2026
Z=us-central1-a
VM=fraud-release-capacity-1126
ID=938503370005181237
target=("$VM" --project "$P" --zone "$Z")
test "$(gcloud compute instances describe "${target[@]}" --format='value(id,status)')" = "$(printf '%s\tTERMINATED' "$ID")"
gcloud compute instances remove-metadata "${target[@]}" --keys=startup-script
gcloud compute instances set-scheduling "${target[@]}" \
  --max-run-duration=4h --instance-termination-action=STOP
gcloud compute instances describe "${target[@]}" \
  --format='yaml(id,status,scheduling.maxRunDuration,scheduling.instanceTerminationAction,metadata.items.key)'
```

The platform limit was applied and verified on the stopped instance on November 27. Readback returned ID `938503370005181237`, `TERMINATED`, `14400` seconds and `STOP`. The operator guard additionally requires removal of the legacy startup script. It starts the VM, bounds the operator command to 210 minutes, then stops and verifies `TERMINATED` on completion, failure or signal. A workstation outage leaves the platform deadline in force. Evidence export must finish inside the operator window. If direct stop cannot be confirmed, run `gcloud compute instances stop "${target[@]}"` and independently confirm `TERMINATED`. Both disks remain retained. [Compute Engine runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime)

## Stage and run the two trials

Before starting, prepare a tar with the repository-relative paths for the new driver, runtime probe, oracle, telemetry, and capacity manifest. Record its SHA-256. The existing host must retain the scanned loopback registry bytes and the checksum-verified seed and model. Use IAP to stage the small tar. Inspect legacy services and timers before continuing. Start only the existing registry if stopped and verified bound to `127.0.0.1:5500`. A missing manifest aborts the run rather than triggering a rebuild. Keep prior trial evidence and volumes intact. Use a unique session ID.

```bash
tar -czf capacity-tools.tgz deploy/run-capacity-rehearsal.sh deploy/capacity-oracle.py \
  deploy/capacity-manifests.txt deploy/capacity-telemetry.sh deploy/check-python-runtime.sh \
  deploy/sample-release-load.sh benchmarks/sustained.py deploy/seed-capacity-baseline.py \
  deploy/compare-capacity-trials.py docker-compose.production.yml
sha256sum capacity-tools.tgz
FRAUD_CAPACITY_START_APPROVED="$ID" bash deploy/with-capacity-vm.sh bash
# Execute the following from the guarded shell. Exit it after export.
P=fraud-detection-platform-2026; Z=us-central1-a; VM=fraud-release-capacity-1126
target=("$VM" --project "$P" --zone "$Z")
gcloud compute scp --tunnel-through-iap --project "$P" --zone "$Z" \
  capacity-tools.tgz "$VM:capacity-tools.tgz"
gcloud compute ssh "${target[@]}" --tunnel-through-iap
```

On the VM, compare `sha256sum ~/capacity-tools.tgz` with the workstation digest. Inspect previously staged timers, services and port bindings. From the IAP session, prepare a fresh checkout and launch the driver:

```bash
sudo systemctl list-units 'fraud-capacity*' --all
sudo docker ps -a --format '{{.Names}} {{.Ports}}'
sudo ss -lntup
sudo -i
set -euo pipefail
test "$(docker inspect --format '{{json .HostConfig.PortBindings}}' release-inventory-registry)" = \
  '{"5000/tcp":[{"HostIp":"127.0.0.1","HostPort":"5500"}]}'
docker start release-inventory-registry
ROOT=/srv/fraud-capacity
SESSION=release-1127a
REPO="$ROOT/repo-$SESSION"
mkdir "$REPO"
git -C "$ROOT/repo" fetch origin d02c9c53a9728d9aad180be82064e7fee6e54404
git -C "$ROOT/repo" archive FETCH_HEAD | tar -x -C "$REPO"
# Replace OPERATOR_HOME with the OS Login home printed before sudo.
tar -xzf /home/OPERATOR_HOME/capacity-tools.tgz -C "$REPO"
mkdir -p "$REPO/artifacts"
cp -a "$ROOT/repo/artifacts/baseline" "$ROOT/repo/artifacts/feature-seed" "$REPO/artifacts/"
sha256sum "$REPO/artifacts/baseline/model.json" "$REPO/artifacts/feature-seed/seed.json"
FRAUD_CAPACITY_REPO="$REPO" FRAUD_CAPACITY_BASELINE_IMAGE_ID="$BASELINE_IMAGE_ID" \
  bash "$REPO/deploy/run-capacity-rehearsal.sh" "$SESSION"
```

Before starting the VM, build the main serving image offline from a clean archive of
`6f1a39f018e4c8f5bb11f9c4b051ba73d6467094`. Set the Docker build label
`org.opencontainers.image.revision` to that full SHA and use `--platform linux/amd64`.
Tag the image `fraud-capacity-main:6f1a39f`, save a checksum-verified archive,
transfer it via IAP and verify that checksum before loading. Set
`BASELINE_IMAGE_ID` from `docker image inspect --format '{{.Id}}' fraud-capacity-main:6f1a39f`
on the guest after load. Docker image-store implementations can report distinct
local identifiers for the same archive. Prefer a staged archive over building
inside the billed runtime.

The driver alternates main-1, release-1, release-2 and main-2 on the same host.
All four runs use fresh projects, the same model and seed, the same backend
images, private ingress and one minute warmup plus ten minutes measured at
each of four client levels. Main uses its fixed Feast project and the driver
verifies the initial seed there. The four workloads consume at least 176 minutes,
including 88 minutes for the two candidate repeats. This leaves at most 34
minutes inside the operator window for IAP staging, startup, reconciliation and
export. Confirm those timings with staged assets before starting. Run the
monitoring fault or coordinated recovery only if the time and spending checks
still pass. Preserve partial evidence if the window closes.

Each repeat produces 27,000 Kafka events at 10/s and runs a PostgreSQL dump,
Redis rewrite and drift with ingestion active. The candidate must have zero
restarts, errors, timeouts, duplicates, missing or unexpected scores, dead
letters and pending updates, exact score and alert reconciliation, verified
Redis/Feast digests, zero final lag, sampled CPU p95 ≤70%, peak memory and
both filesystems ≤70%, and healthy AOF. Review PostgreSQL counters and disk I/O.
The comparison checks the original 95% throughput and 110% latency limits.
Verify the deployed 600/minute limiter separately. [Throughput acceptance](throughput-acceptance.md)

Local Compose v2.40.3 rendered eight distinct pinned image roles, three consumer replicas, and a single published loopback port at `127.0.0.1:18443`. Mocked resource gates accepted 70% CPU p95 with 80 measured samples and rejected both 71% and a 69-sample run. Local guard simulations exercised approval denial, stale startup script denial, overlong schedule denial, and shutdown on success and command failure. Guest probes recovered from injected endpoint, Kafka, journal and Redis fixture errors. The notification verifier required two successfully acknowledged states for the same incident in the fixture. These checks leave both sustained workload execution and real cloud notification delivery open.

Before exiting the guarded shell, export the allowlisted evidence logs and JSON via IAP SCP. Keep the SQL dump and all historical evidence on retained storage. In the root guest shell, replacing `OPERATOR_USER` and its home with the OS Login values:

```bash
cd "/srv/fraud-capacity/evidence/trials/$SESSION"
find . -type f \( -name '*.log' -o -name '*.err' -o -name '*.txt' -o -name '*.json' \
  -o -name '*.jsonl' -o -name complete \) -print0 |
  tar -czf "/home/OPERATOR_USER/$SESSION-evidence.tgz" --null -T -
test "$(stat -c %s "/home/OPERATOR_USER/$SESSION-evidence.tgz")" -le 1073741824
chown OPERATOR_USER "/home/OPERATOR_USER/$SESSION-evidence.tgz"
chmod 0600 "/home/OPERATOR_USER/$SESSION-evidence.tgz"
sha256sum "/home/OPERATOR_USER/$SESSION-evidence.tgz"
```

From the guarded workstation shell, run `gcloud compute scp --tunnel-through-iap --project "$P" --zone "$Z" "$VM:release-1127a-evidence.tgz" ./release-1127a-evidence.tgz`, verify the checksum, then exit the guarded shell. Run `gcloud compute instances describe "${target[@]}" --format='value(status)'` and confirm `TERMINATED`.

## Automated incident and recovery verification

The driver emits structured guest samples every 30 seconds for HTTPS readiness, Kafka lag, stale Feast journal, Redis AOF and filesystem usage. The Ops Agent file receiver and distribution metric support explicit healthy zeros and both OPENED and CLOSED notifications. The approved `fraud-capacity-telemetry` identity is attached to VM `938503370005181237` with project `roles/logging.logWriter` and `roles/monitoring.metricWriter` and no keys. Guest Ops Agent 2.73 uses the [receiver configuration](../deploy/capacity-ops-agent.yaml). Policy `10793285701053752654` uses existing private channel `14251330304105084080` and topic `fraud-rehearsal-alerts`. Verify recorded samples and both transitions during the controlled incident. The commands below record the setup for reconstruction after cleanup.

```bash
SA="fraud-capacity-telemetry@$P.iam.gserviceaccount.com"
gcloud iam service-accounts create fraud-capacity-telemetry --project "$P"
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$P" --member="serviceAccount:$SA" --role="$role"
done
gcloud compute instances set-service-account "${target[@]}" \
  --service-account="$SA" \
  --scopes=https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write
CHANNEL=projects/$P/notificationChannels/14251330304105084080
gcloud logging metrics create fraud_capacity_guest_fault --project "$P" \
  --config-from-file=deploy/capacity-log-metric.yaml
jq --arg channel "$CHANNEL" '.notificationChannels=[$channel]' \
  deploy/capacity-alert-policy.json > /tmp/fraud-capacity-alert.json
gcloud monitoring policies create --project "$P" --policy-from-file=/tmp/fraud-capacity-alert.json
gcloud pubsub subscriptions create fraud-capacity-verifier-1127 --project "$P" \
  --topic=fraud-rehearsal-alerts --expiration-period=1d
python3 deploy/verify-capacity-notifications.py \
  --policy-name 'Fraud capacity rehearsal guest fault' --timeout 600
```

For the agent-enabled path, block and verify access to Compute Engine metadata from workload container networks before starting the application stack. Host-side Ops Agent authentication must continue to work. Use an approved host firewall rule on the Docker forwarding chain and verify from a disposable container on the same network. Grant neither registry-write nor secret permissions to the telemetry identity. Plan the notification test outside the 88 minutes of measured capacity work.

Install `deploy/block-container-metadata.sh` in a root-owned directory via a
Docker service `ExecStartPre` drop-in before attaching the identity. Verify the
IPv4 Docker forwarding hook and both reject rules. The pilot verified that a
container could not open the metadata socket whilst the guest could fetch its
instance ID. Rerun `deploy/check-container-metadata.sh` across every active
workload network namespace and probe the drift one-off before it starts. The
agent receives only the structured capacity file in this rehearsal. Once
stopped, detach the identity if no follow-up run is scheduled. Preserve the
drop-in, original agent configuration backup and all evidence. Disable the test
policy when no further tests are authorized. The verifier subscription has a
one-day inactivity expiry.

Use a separate isolated private fixture or run after measured load ends. Leave
the probe and its log receiver running. Use the verifier in a separate
operator shell and begin with repeated healthy samples. From the guest shell
with the same fully specified fixture Compose array, run
`"${stack[@]}" stop api` until OPENED, then
`"${stack[@]}" up -d --wait --no-build api` and verify CLOSED. Allow at least
one complete aligned minute of failed probes and check that the restored
probes report zero. Preserve both acknowledged messages with the same
incident ID, policy and instance. Review live CPU, memory, DLQ, lag,
PostgreSQL, AOF and journal data independently of this composite
notification. [Monitoring notification strategy](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies)

## Private manifest rollout and rollback

[The manifest allowlist](../deploy/capacity-manifests.txt) matches the retained private scan for the earlier build. The [d02c9c5 hosted inventory](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45960469960) records a different set from ephemeral CI registries. Neither set is a published deployment reference. The stopped guest's registry is not reachable until approved start. Verify every digest by pull before load. After separate promotion approval, use an immutable same-region private repository, `skopeo copy --preserve-digests`, and a repository-scoped pull identity. Verify every destination digest against the signed approval and apply the [image-only override](../deploy/release-images.example.yml) with `pull` and `up --no-build`. Keep Caddy on loopback and retraining disabled. Prepare the previous verified digest override and record volume and model identities. Quiesce producers, ingress and writers, drain the journal, capture the coordinated checkpoint, then exercise a failed upgrade and restore. If any schema changed incompatibly, restore to fresh isolated volumes and reconcile offsets, scores, alerts, Redis state and the MLflow alias/artifact before resuming. [Coordinated recovery procedure](production-readiness.md#backup-verification-and-rollback)

After separate approval for publication to a private repository, run from the
authorized environment with access to the retained registry, replacing
`APPROVED_RELEASE_TAG` with the owner's immutable tag:

```bash
gcloud artifacts repositories create fraud-release --project "$P" --location us-central1 \
  --repository-format=docker --immutable-tags
gcloud auth print-access-token | skopeo login --username oauth2accesstoken \
  --password-stdin us-central1-docker.pkg.dev
while read -r role source; do
  [[ $role != retrain ]] || continue
  [[ $source == 127.0.0.1:5500/fraud-release/*@sha256:* ]]
  digest=${source##*@}
  target_ref="us-central1-docker.pkg.dev/$P/fraud-release/$role:APPROVED_RELEASE_TAG"
  skopeo copy --preserve-digests --src-tls-verify=false "docker://$source" "docker://$target_ref"
  test "$(skopeo inspect --format '{{.Digest}}' "docker://$target_ref")" = "$digest"
done < deploy/capacity-manifests.txt
```

Gate deployment on preserved exact bytes and a separately recorded prior
known-good override. Once the approved current and rollback image-only
overrides contain literal, verified digest references, stage both in the
root-owned deployment directory. After quiescing writers and checkpointing
state, use the following private Compose sequence:

```bash
private=(sudo bash deploy/compose.sh -p fraud-private --profile registry --profile monitoring)
next=( "${private[@]}" -f /etc/fraud/release-images.approved.yml -f /etc/fraud/loopback.yml )
prior=( "${private[@]}" -f /etc/fraud/release-images.previous.yml -f /etc/fraud/loopback.yml )
"${next[@]}" config --images
"${next[@]}" pull
"${next[@]}" up -d --no-build --wait postgres redis kafka
# Promote the verified seed if bootstrapping fresh state. Verify the journal and checkpoint before writers.
"${next[@]}" up -d --no-build --wait api consumer dashboard mlflow proxy
# For a compatible rollback, first stop ingress and writers and re-verify the checkpoint.
"${next[@]}" stop proxy api consumer
"${prior[@]}" config --images
"${prior[@]}" pull
"${prior[@]}" up -d --no-build --wait postgres redis kafka api consumer dashboard mlflow proxy
```
