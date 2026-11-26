# Release image security checkpoint

**Production release blocked.** This assessment covers the `linux/amd64` manifests from `f1c619eab9636eca751d501a7edbe1accb9a37cb`. The scan retained all severities without release-image ignores. Counts are binary-package/CVE pairs. The five Python images account for 22 distinct high-severity CVEs. All nine images have zero critical findings. [Raw inventory, scan and manifest artifacts](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45597573010)

| Image | SHA-256 manifest digest | High |
| --- | --- | ---: |
| Serving (API and consumers) | `0c6336a4483323d7c6f3bc6f91efb61fadac7caf5278d36a242664095d542218` | 53 |
| Dashboard | `c1fbde1819255e32ae1643acc6f03464123cf1caca5961033b36902216b05ceb` | 53 |
| Drift | `c89768cb55d60fe1206b9db1c896b269e3eced9ea1bd0a66fff5577dce0a6cc8` | 53 |
| MLflow | `440e18e921fd461af5cdbff85b337ce664b2851cd0facbc847196fd7a966b8f7` | 53 |
| Retrain | `7eefd11d4c364b882468fbd916345e94f1c306aa913785ea955bb69214feafd5` | 68 |
| Kafka | `022911bd5239473c44189ee2bfc5ecf7074daa5e4c49a20c15d295a45ac15aff` | 0 |
| PostgreSQL | `fa2f902bc52d03f1590ae718a3df0d7097558d955447fb2844b792edca9e67ce` | 0 |
| Redis | `e4ee3e03ce8f75471f40937a7c6b2b0e500390b6812fcdf5cb32c01295ded446` | 0 |
| Caddy | `0c0eba2013316469ab9f5a5cf1ba697ae510b0ade15dcf45564c14ba62dab449` | 0 |

The November 26 scan at `9385799` reported two new high-severity Redis package
findings for the same OpenSSL advisory, CVE-2026-124709. The Redis build retains
7.4.12 and requires Alpine's fixed `libcrypto3` and `libssl3` revisions
`3.5.9-r2` or later. Both packages resolved to `3.5.9-r2` in the local build.
The patched Redis digest `sha256:429dd7fcc0903e3fb6f5638f02ac0d4550cbef69913cfdb8c10029366f3064db`
had zero high and critical findings. A local AOF-backed restart retained its fixture.
The hosted recovery script now builds Redis with the other local images before starting with
`--no-build`. The Python image findings below remain open.
[Initial scan](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45733807628) [Patched scan](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45736248107) [Alpine package index](https://dl-cdn.alpinelinux.org/alpine/v3.22/main/x86_64/)

## Applicability review on 2026-11-25

The `66f75ca` inventory contains the exact manifest and each installed package
version, affected image and scanner fix field for the 22 findings below. The follow-up
review found no supported Trixie fixed version to apply from that inventory or the
linked Debian records. Production Compose now drops all application capabilities
and sets no-new-privileges. The recovery harness checks both settings in the running
serving process. Retraining remains an opt-in profile. All findings remain open
pending a supported fix or a time-limited release-owner decision. No residual-risk
approval or expiry is recorded for an open finding.
[Exact package and image inventory](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45739147563) [Debian stable tracker](https://security-tracker.debian.org/tracker/status/release/trixie)

The table combines the exact Trivy binary inventory with the Debian source-package advisory and the production Compose call paths. Each listed Trixie issue was open without a fixed release version in Debian's tracker. Presence of a binary package is confirmed by the scan. Reachability statements reflect code and configuration review and remain subject to runtime confirmation. No finding was suppressed, waived or accepted. [Debian release tracker](https://security-tracker.debian.org/tracker/status/release/trixie)

| Debian source and image scope | CVE | Vulnerable component, current constraints and remaining verification |
| --- | --- | --- |
| util-linux, all five | [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | Privileged post-hooks after mount helper failure. No application mount action or privileged Python container is defined. Confirm effective capabilities, setuid bits and host/bind-mount boundaries. |
| util-linux, all five | [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | Privileged `nsenter --join-cgroup` against an attacker-controlled target. No application namespace entry is configured. Keep that operator action outside workload containers. |
| util-linux, all five | [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | Detached-tree mount subdirectory resolution. Requires authorized mount configuration and a suitable kernel. No workload mount entries are configured. Confirm host mount paths and effective capabilities. |
| util-linux, all five | [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | Restricted bind mount with ownership/mode hooks and a writable authorized source. Docker sets mounts from the host. Keep the operator configuration and source ancestors protected and verify workload privileges. |
| acl, all five | [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | Privileged pathname-based ACL calls through attacker-writable paths. Images start as UID 10001. Review any privileged backup or maintenance ACL operations on writable shared paths. Debian plans a point-release fix. |
| ncurses, all five | [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | `infocmp` parser in `ncurses-bin`. No workload entrypoint invokes it. Avoid external terminfo or arbitrary operator commands, and confirm default startup and subprocess traces. |
| SQLite, all five | [CVE-2026-119373](https://security-tracker.debian.org/tracker/CVE-2026-119373) | FTS5 prefix query after attacker-controlled FTS data. MLflow actively uses SQLite, and Feast has a SQLite configuration outside the production Redis path. Confirm compile options and trace schemas, migrations and any reachable FTS queries. |
| SQLite, all five | [CVE-2026-119374](https://security-tracker.debian.org/tracker/CVE-2026-119374) | Corrupt FTS5 index and secure-delete path. Require trusted, checksum-verified database restores. Verify actual FTS5 schema, compile options, and restore/import ingress in MLflow. |
| SQLite, all five | [CVE-2026-119379](https://security-tracker.debian.org/tracker/CVE-2026-119379) | SQLite session changeset application. Confirm build options and native call sites, and keep changeset or SQLite-file uploads outside supported ingress. |
| OpenSSL, all five | [CVE-2026-107494](https://security-tracker.debian.org/tracker/CVE-2026-107494) | QUIC endpoint can exhaust memory under a malicious peer. Caddy terminates ingress TLS and Python services use private HTTP/TCP. Audit outbound dependency QUIC use and loaded native libraries. |
| OpenSSL, all five | [CVE-2026-124709](https://security-tracker.debian.org/tracker/CVE-2026-124709) | QUIC server stream-state leak. No Python QUIC listener is configured. Confirm UDP listeners and keep QUIC termination out of the Python workloads. |
| systemd, all five | [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | Requires `systemd-homed` and a logged-in managed user. Inventory has `libsystemd0` and `libudev1`, but no homed package or service is configured. Verify file ownership and absence in the rootfs. Assess the guest OS separately. |
| Perl, all five | [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | `Archive::Tar` untrusted archive parsing. `perl-base` is present in all images; full Perl packages occur in retrain. Confirm module availability and invocation in each image. Treat recovery archives as trusted, verified operator inputs. |
| Git, retrain | [CVE-2026-105437](https://security-tracker.debian.org/tracker/CVE-2026-105437) | Three-way textual merge. Tracking calls `git status` and `git rev-parse` on a read-only trusted checkout. Keep merge, rebase and cherry-pick out of the job. |
| Git, retrain | [CVE-2026-111760](https://security-tracker.debian.org/tracker/CVE-2026-111760) | Recursive clone of untrusted submodules. No clone appears in model tracking. Restrict repo provenance and verify startup performs no Git transport operations. |
| Git, retrain | [CVE-2026-114466](https://security-tracker.debian.org/tracker/CVE-2026-114466) | Git fast-import/export unsafe features. No application import/export path is configured. Keep repository import outside retraining and restrict allowed invocations. |
| curl, retrain | [CVE-2026-12064](https://security-tracker.debian.org/tracker/CVE-2026-12064) | curl CLI SFTP/SCP scheme-inference path. The scan flags the GnuTLS shared library; confirm ownership and absence of the CLI. Do not supply user-controlled SFTP/SCP endpoints to retraining. |
| curl, retrain | [CVE-2026-8458](https://security-tracker.debian.org/tracker/CVE-2026-8458) | libcurl Negotiate connection-cache reuse. Review actual native callers and credential contexts. No Negotiate endpoint is configured for the retraining job. |
| Expat, retrain | [CVE-2026-113351](https://security-tracker.debian.org/tracker/CVE-2026-113351) | Reused native XML parser with reset. No XML route is configured. Check both the OS library and CPython's parser linkage and job inputs. |
| Expat, retrain | [CVE-2026-115506](https://security-tracker.debian.org/tracker/CVE-2026-115506) | XML DTD and entity amplification. Restrict report inputs to configured trusted formats and confirm no reachable DTD parser. |
| Expat, retrain | [CVE-2026-117610](https://security-tracker.debian.org/tracker/CVE-2026-117610) | Native external-entity subparser. Confirm whether any XML consumer enables external entities, including dependencies. |
| Expat, retrain | [CVE-2026-118370](https://security-tracker.debian.org/tracker/CVE-2026-118370) | Oversized XML attribute count. Keep XML outside job inputs and verify resource bounds and any indirect parsers. |

The shared binary groups are `bsdutils libblkid1 liblastlog2-2 libmount1 libsmartcols1 libuuid1 login mount util-linux`, `libacl1`, `libncursesw6 libtinfo6 ncurses-base ncurses-bin`, `libsqlite3-0`, `libssl3t64 openssl openssl-provider-legacy`, `libsystemd0 libudev1`, and `perl-base`. Retrain additionally includes `git git-man libcurl3t64-gnutls libexpat1 libperl5.40 perl perl-modules-5.40`. Installed versions and package IDs are in each `trivy.json` and `high-critical.tsv`. The Python Dockerfile uses a non-root application user. The production overlay sets no-new-privileges on API, consumer, dashboard, MLflow, drift and retrain. The scan's `python-runtime.txt` probe uses the same resolved manifests in a separate networkless, capability-free container. Its privilege readings describe the probe, while deployed security settings must be verified independently.

## Patch and release decisions

Debian Forky/Sid lists fixes for 17 of the 22 issues, including SQLite 3.54.1, Expat 2.10.0, util-linux 2.42.3/2.42.4 and ACL 2.4.0. It leaves the three Git issues and both OpenSSL issues open. Testing or unstable packages mixed into Trixie would change the runtime and support model. Use fixed Trixie packages or signed, supportable backports when available. A different maintainable base needs a separate digest scan and a wheel/native-library compatibility, coordinated restore and retraining check. Reverting to Bookworm would reopen the previously observed critical OS findings.

Keep retraining disabled while its Git and XML risks are reviewed. Before removing Git, supply equivalent verified checkout provenance for training. Check whether MLflow can migrate to a supported PostgreSQL backend as a separate tested change with alias and artifact parity. That migration alone would leave the OS package installed. For release, verify per-digest executable and library ownership with `dpkg-query -L`, inspect runtime subprocesses and listeners, query SQLite compile options and schemas, and have the designated release/security owner review the evidence and any expiry-bound residual acceptance. Otherwise keep the production gate blocked.

After any image change, repeat the unfiltered digest scan, full hosted CI, and the targeted coordinated restore, Kafka continuity and Feast byte parity checks. Bind the resulting evidence to the deployable manifests and verify the registry digests after publication. [Hosted regression at f1c619e](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45597573046) [Kafka compatibility](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45591965673) [Feast byte parity](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45594638604) [Coordinated patched-stack recovery](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45597573128)
