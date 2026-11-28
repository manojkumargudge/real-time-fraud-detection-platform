# Release image security checkpoint

## November 28 review of the enabled Python images

The unfiltered Linux/amd64 inventory on `d140326` has identical High and
Critical package/CVE rows to the previously downloaded `9b4b24f` inventory.
These temporary CI manifests identify scanned bytes. The release decision
requires a fresh comparison with the exact bytes retained for publication.
[Unfiltered scans and runtime probes](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/46119116365)

| Enabled image | SHA-256 manifest |
| --- | --- |
| Serving | `edd7ed15f7e0011ee554088e460417654b7c5848b956d2ca0a6697c3a3ad45a7` |
| Dashboard | `f6b5bf0511ceb5f3f71ff52efa56a2933e3243b76a94c6764a3cc91e444cfa0d` |
| Drift | `75c07e14ccbb475456493e28999988951b434abfd620dae3566b1639c4abd924` |
| MLflow | `10e34c90a819a3cdda8e3a6f0dd68cc3e49a58d592e9cfebad68fccb6ad2cf7c` |

A disposable serving container refreshed signed Trixie and security indexes.
The representative packages from all seven affected source groups still
resolved to their installed versions. No stable upgrade cleared these
findings. The earlier backport and alternate base checks below document the
supported alternatives already considered. The scanner recorded every row
below as High. Each remains subject to the release-owner policy.

| CVE | Affected installed package group | Reachability and controls |
| --- | --- | --- |
| CVE-2026-76642 | util-linux group¹ | Privileged mount hooks require a mount path. The Compose application roles drop all capabilities and set no-new-privileges. Keep privileged mounts on the host. |
| CVE-2026-78408 | util-linux group¹ | Namespace and cgroup administration require privileges absent from normal application roles. The executable remains installed. |
| CVE-2026-78409 | util-linux group¹ | Detached-tree mount handling requires a privileged mount path. No application service uses such a path in Compose. |
| CVE-2026-78410 | util-linux group¹ | The risky path requires privileged bind-mount handling. Compose mounts are operator-controlled. Host source ownership and mount procedures still matter. |
| CVE-2026-54369 | libacl1 `2.3.2-2+b1` | ACL symlink handling can cross privilege boundaries. Non-root containers restrict impact. Privileged backup and staging ACL paths need review. |
| CVE-2025-69720 | ncurses and libtinfo `6.5+20250216-2` | Terminal tooling remains installed. No serving command invokes `infocmp`. Operator terminal inputs and subprocesses remain outside the runtime evidence. |
| CVE-2026-119373 | libsqlite3-0 `3.46.1-7+deb13u2` | FTS5 is compiled in. Use trusted MLflow databases and backups. Exact deployment schema and imported artifact paths need verification. |
| CVE-2026-119374 | libsqlite3-0 `3.46.1-7+deb13u2` | Corrupted FTS5 index handling requires FTS5 input. The internal registry and trusted restore boundary reduce exposure. Exact schema verification remains open. |
| CVE-2026-119379 | libsqlite3-0 `3.46.1-7+deb13u2` | SQLite session support is present. No configured changeset importer was identified. Restore inputs must remain controlled. |
| CVE-2026-107494 | openssl, libssl3t64 and openssl-provider-legacy `3.5.9-1~deb13u1` | The affected feature is OpenSSL QUIC. Caddy owns public TLS and the Python service configuration uses TCP. Exact final listener and dependency traces remain open. |
| CVE-2026-124709 | openssl, libssl3t64 and openssl-provider-legacy `3.5.9-1~deb13u1` | The affected feature is OpenSSL QUIC stream processing. The Compose roles expose no Python QUIC endpoint. Check actual listeners before release. |
| CVE-2026-16742 | libsystemd0 and libudev1 `257.13-1~deb13u1` | Exact inventory probes did not find `systemd-homed` on PATH. The affected service is absent from the Compose entrypoints. Host OS assessment is separate. |
| CVE-2026-9538 | perl-base `5.40.1-6+deb13u1` | `Archive::Tar` import failed in each enabled Python image's inventory probe. Keep archive inputs trusted and verify the final image and its maintenance commands. |

¹ `libblkid1`, `liblastlog2-2`, `libmount1`, `libsmartcols1`, `libuuid1`,
`mount` and `util-linux` are `2.41.5-0+deb13u1`. `bsdutils` is
`1:2.41.5-0+deb13u1`. `login` is `1:4.16.0-2+really2.41.5-0+deb13u1`.

**Disposition for all 13 findings is hold.** Signed stable indexes offered
no verified fixed candidates. Mitigations are conditional on the reviewed
Compose controls and trusted operator inputs. Release requires verified
fixed bytes or explicit policy disposition by the release owner.

## Release-owner decision for the audited candidate

**Recommendation: keep release blocked.** The exact nine-image inventory at
`333ad04f8a940cedc7b49eba3b754d5a553af406` shows zero critical findings and
22 distinct high findings. Zero of those 22 have a verified fix in this
candidate. The full unfiltered inventory has the binary-level evidence.
[Exact inventory and runtime probes](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45755631901)

| Image role | Scanned linux/amd64 manifest |
| --- | --- |
| caddy | `sha256:fa6c4ab3474a3876aec6636cd54a8c6f6250b2885ec5be497b41a06725944426` |
| dashboard | `sha256:c754e285954b0e4ee1b9c5261b39c5ad61106919d68988b886e5d30270361eba` |
| drift | `sha256:543f76b8b22c4479ba64a75fb0493c8a78e2946faf1e2c884d5b2c4240e2d1c2` |
| kafka | `sha256:d6b1d76098060574a7f99cbb9358ba1934f7310659ac9b4db6c36d02e0ee2f21` |
| mlflow | `sha256:dd0c2ec73aa97b116382f0054b85147dc8e73ae58ba84be092b03d1054213169` |
| postgres | `sha256:801f11b19c53a20a97cab34a957bc107948518daee1bda6b8a4c334a1fa311a9` |
| redis | `sha256:4b9568b3fb27df712e5a0f65d266b11b9c5094b7486bc71325d48ef3f605b438` |
| retrain | `sha256:40cde07be7337e7dd44af3d2e2967aa49e58940d2198cdee2c7b33588dc15566` |
| serving | `sha256:23f3e1202bab6dd8c3f26cde4493c7c6af84a3660019f09b25007a85897ab3b9` |

These references came from a temporary CI registry. The manifests identify the
scan evidence. The deployment registry still needs verified copies and pull access.

### Supported remediation checks on November 26

- A disposable official Python 3.11 Trixie image refreshed signed stable and
  security indexes. Installed release versions match the available candidates
  for the affected package groups listed below. Debian's current tracker still
  marks all 22 stable Trixie entries vulnerable. The signed official Trixie
  backports archive supplies curl 8.21.0 for the two retraining findings. The
  isolated installation retained stable Git and resolved its transport to
  the upgraded GnuTLS library. Its final-image scan and retraining validation
  govern whether that candidate can replace the baseline below.
  A newer Python image refresh on this Debian suite would still
  need the affected packages to change. [Debian stable tracker](https://security-tracker.debian.org/tracker/status/release/trixie) [Official Python images](https://hub.docker.com/_/python)
- An isolated APT removal simulation in the cached Trixie serving image failed
  on dependencies from essential packages including coreutils and tar.
  Simulating removal of `ncurses-bin` alone flagged it as essential, with the
  affected source's other installed binary packages still present. Multi-stage
  removal of compilers and Git cannot eliminate affected shared libraries
  that remain in the runtime image. Git and its dependency chain are confined
  to the optional retraining build in the exact inventory.
- A disposable official `python:3.11-alpine` container could not resolve a
  binary wheel for pinned `xgboost-cpu==2.1.4`. That candidate stopped before
  application startup. The pinned PyArrow musllinux wheel did resolve
  when checked separately. A source rebuild or a curated glibc runtime needs
  separate provenance, native-library, wheel, parity and coordinated
  recovery evidence. The existing Bookworm image history includes critical
  findings. The current Trixie image stays in place.

### Review worksheet

All rows below are scanner **High**. “Shared” means serving, dashboard, drift,
MLflow and retrain, with exact image digests above. Current supported fix
availability is **none verified** except for the two curl backport candidates.
Owner for any future exception
is the release owner, and the proposed latest expiry is **2026-12-10**, or
earlier upon a supported fix or any image, ingress or privilege change. This
date is a proposal. No exception has been granted.

| CVE | Affected binary packages and installed version | Scope | Recommended disposition and remaining risk |
| --- | --- | --- | --- |
| CVE-2026-76642 | util-linux group¹ | Shared | **AFFECTED BUT MITIGATED** for serving under the verified capability and privilege controls. Hold the other roles until their effective controls are attested. A privileged maintenance mount remains outside this finding's containment. |
| CVE-2026-78408 | util-linux group¹ | Shared | **AFFECTED BUT MITIGATED** for serving. Namespace entry needs privileged context. Verify the other roles and operator mount boundaries before acceptance. |
| CVE-2026-78409 | util-linux group¹ | Shared | **AFFECTED BUT MITIGATED** for serving. Keep detached-tree mounts outside application containers and verify the other roles. |
| CVE-2026-78410 | util-linux group¹ | Shared | **AFFECTED BUT MITIGATED** for serving. Operator bind-mount and ownership changes remain a separate risk. Verify other roles and source path permissions. |
| CVE-2026-54369 | libacl1 2.3.2-2+b1 | Shared | **INSUFFICIENT EVIDENCE.** The non-root serving control lowers exposure. Review privileged ACL use on writable backup and bind-mount paths. |
| CVE-2025-69720 | libncursesw6, libtinfo6, ncurses-base, ncurses-bin 6.5+20250216-2 | Shared | **INSUFFICIENT EVIDENCE.** No configured infocmp entrypoint. Verify process traces and trusted terminal inputs. |
| CVE-2026-119373 | libsqlite3-0 3.46.1-7+deb13u2 | Shared | **INSUFFICIENT EVIDENCE.** FTS5 is compiled in. Establish whether any runtime schema and trusted restore path can introduce FTS5 content. |
| CVE-2026-119374 | libsqlite3-0 3.46.1-7+deb13u2 | Shared | **INSUFFICIENT EVIDENCE.** FTS5 is compiled in. Verify schemas and restrict restore inputs to checksum-verified archives. |
| CVE-2026-119379 | libsqlite3-0 3.46.1-7+deb13u2 | Shared | **INSUFFICIENT EVIDENCE.** Session support is compiled in. Review changeset callers and import boundaries. |
| CVE-2026-107494 | libssl3t64, openssl, openssl-provider-legacy 3.5.9-1~deb13u1 | Shared | **INSUFFICIENT EVIDENCE.** Caddy owns public TLS and Python routes use TCP configuration. Verify effective listeners and outbound dependency behavior before acceptance. |
| CVE-2026-124709 | libssl3t64, openssl, openssl-provider-legacy 3.5.9-1~deb13u1 | Shared | **INSUFFICIENT EVIDENCE.** No Python QUIC endpoint appears in Compose. Confirm actual UDP listeners. The patched Redis image is outside this Python finding. |
| CVE-2026-16742 | libsystemd0, libudev1 257.13-1~deb13u1 | Shared | **INSUFFICIENT EVIDENCE.** Probe lookup did not find systemd-homed in PATH. Verify its absence from the full rootfs and assess guest OS separately. |
| CVE-2026-9538 | perl-base 5.40.1-6+deb13u1, plus libperl5.40, perl and perl-modules-5.40 at that version in retrain | Shared | **INSUFFICIENT EVIDENCE.** Archive::Tar was importable in retrain only. Keep retrain disabled and verify archive inputs and Perl invocation. |
| CVE-2026-105437 | git, git-man 1:2.47.3-0+deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Restrict Git to trusted checkout provenance commands and confirm no merge subprocess. |
| CVE-2026-111760 | git, git-man 1:2.47.3-0+deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Verify Git never clones or recursively imports untrusted repositories. |
| CVE-2026-114466 | git, git-man 1:2.47.3-0+deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Verify the job cannot invoke Git fast import or export. |
| CVE-2026-12064 | libcurl3t64-gnutls 8.14.1-2+deb13u7 | Retrain | **INSUFFICIENT EVIDENCE** for accepting the baseline. The 8.21.0 backport contains the upstream fix. Keep retraining disabled until the exact candidate passes. [Upstream advisory](https://curl.se/docs/CVE-2026-12064.html) |
| CVE-2026-8458 | libcurl3t64-gnutls 8.14.1-2+deb13u7 | Retrain | **INSUFFICIENT EVIDENCE** for accepting the baseline. The 8.21.0 backport contains the upstream fix. Keep retraining disabled and verify its exact image. [Upstream advisory](https://curl.se/docs/CVE-2026-8458.html) |
| CVE-2026-113351 | libexpat1 2.9.0-1~deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Determine which Expat version each XML consumer loads and whether any reset path is used. |
| CVE-2026-115506 | libexpat1 2.9.0-1~deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Review actual XML and DTD inputs before acceptance. |
| CVE-2026-117610 | libexpat1 2.9.0-1~deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Verify external entity parser call sites and artifact ingress. |
| CVE-2026-118370 | libexpat1 2.9.0-1~deb13u1 | Retrain | **INSUFFICIENT EVIDENCE.** Keep disabled. Trace XML input and resource limits. |

¹ `libblkid1 liblastlog2-2 libmount1 libsmartcols1 libuuid1 mount util-linux`
are `2.41.5-0+deb13u1`; `bsdutils` is `1:2.41.5-0+deb13u1` and `login`
is `1:4.16.0-2+really2.41.5-0+deb13u1`.

The exact-image probes observed UID 10001, zero effective capabilities,
no-new-privileges, loaded SQLite/OpenSSL libraries, and SQLite `ENABLE_FTS5`
and `ENABLE_SESSION`. The coordinated recovery harness independently checked
the actual serving container's UID, capability bounding set and no-new-privileges,
verified that backends published no host ports, and exercised trusted
checkpoint and model restoration. Exact all-role listener and subprocess
traces, runtime filesystem ownership checks, and the deployed MLflow SQLite
schema census remain open. Keep those checks separate from the networkless
image probes. [Recovery workflow](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45755631705)

Remediate open findings with a supported stable update or a separately validated
runtime and repeat the exact-digest scan. Any exception requires explicit approval
from the release owner for the stated scope, evidence and expiry. Reachability
and package background for each row are in the earlier analysis below.

## Earlier audit

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

### November 27 package availability

The current Debian tracker still lists all 22 entries open on stable Trixie.
A fresh signed APT index refresh in a disposable amd64 Python Trixie container
checked stable, stable security and official Trixie backports. Curl remains the
only affected source with a verified fixed backport. The stable OpenSSL
candidate is already installed in the audited release images. No additional
supported package upgrade was identified for the remaining 20 findings.
[Debian tracker data](https://security-tracker.debian.org/tracker/data/json)

| Source package | Remaining findings | Fixed release in Debian testing/unstable |
| --- | ---: | --- |
| util-linux | 4 | `2.42.3-1` for CVE-2026-76642, CVE-2026-78409 and CVE-2026-78410. `2.42.4-1` for CVE-2026-78408. |
| acl | 1 | `2.4.0-1` |
| ncurses | 1 | `6.6+20251231-1` |
| sqlite3 | 3 | `3.54.1-1` |
| openssl | 2 | Open in the tracker |
| systemd | 1 | `261.2-1` |
| perl | 1 | `5.42.3-1` |
| git | 3 | Open in the tracker |
| expat | 4 | `2.10.0-1` |

The inventory now records sensitive executable paths and setuid/setgid files
in the network-isolated Python images. The retraining smoke check verifies
that the resolved Git HTTP helper library belongs to the fixed Debian package.
Live role-specific listeners, subprocesses and the MLflow schema still need
verification against the deployment digests before residual-risk approval.

### Retraining libcurl backport

The hosted inventory at `fdc66d0c6892a42665367cc66a5465fc0385c34b` recorded 22 distinct High CVEs and zero Critical CVEs across nine Linux amd64 images. Only the optional retraining image installs Git and its GnuTLS libcurl dependencies. Its smoke test verified the installed `8.21.0-2~bpo13+1` package, Git, the Git HTTP helper's linked libcurl and the required native Python imports. The unfiltered scan still reports CVE-2026-12064 and CVE-2026-8458 against both libcurl binary package names. Both upstream advisories identify 8.21.0 as fixed, and Debian publishes that source version in signed Trixie backports. Record these two findings as patched by version evidence, with scanner reconciliation pending for the deployment digest. The other 20 High findings remain open. Retraining stays disabled. [Hosted inventory and smoke test](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45765828299) [Debian backport](https://packages.debian.org/source/trixie-backports/curl) [CVE-2026-12064](https://curl.se/docs/CVE-2026-12064.html) [CVE-2026-8458](https://curl.se/docs/CVE-2026-8458.html)

The same head passed 712 unit tests, 54 service-backed integration tests, 11 targeted Kafka tests, and the Feast byte-parity workflow. The coordinated recovery fixture recorded a 3-second cold backup, 30-second verified restore and 11-second rollback. These are isolated hosted measurements. Production stays blocked pending the final capacity, exact deployment digest and security disposition gates. [Full CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45765828293) [Kafka](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45765828243) [Feast](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45765828157) [Recovery](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45765828273)

### Private Linux rehearsal inventory

The private eight-vCPU build used the same source commit and a loopback registry. Its unfiltered amd64 scan reproduced the hosted totals of 22 distinct High and zero Critical findings. The counts below are package/CVE rows, which can include the same CVE for multiple packages. The private registry holds the corresponding manifest bytes. Any authorized promotion must preserve the digests and verify them in the destination registry before referencing them in the deployment override.

| Role | Exact SHA-256 manifest | High rows |
| --- | --- | ---: |
| Serving | `3506b463882cf69f97ddd6b5249cccb341bd5585a2318bfa3cc5a13928b7f4b1` | 53 |
| Dashboard | `a68b21b995a84f3585dda56d95fa2ae460880bebdbf4370aff8d45edb167b5b5` | 53 |
| Kafka | `4cdd618a39bef86a4149929e0920c1fe4dad7da54a9a1ddfd85f2b3c9351505e` | 0 |
| PostgreSQL | `9b6c495c503287a796d86bbd38c341c62d10cf58ac65348ea3bb2c16ab993176` | 0 |
| Redis | `4e151855f8a6c663f112045070a1166142cded771ed064f418c6f715bd1f7e78` | 0 |
| MLflow | `343315a4257603ccd3fdaa37c81a9aaa7ba9895b6d6539d2d5ee75311634b92a` | 53 |
| Caddy | `c90a08d80b38b6ef71778f4b372f05dc89dcad4a2a9d616857df79ca271ecfd8` | 0 |
| Drift | `ff23ef4f56956901706d7ad8257566d74bd532354fa461dc9f99df3088781025` | 53 |
| Retrain, disabled | `5687ff5347881f7e11ea962d4f9775540e368ba488777d7fb3ddd65ba9cd55dd` | 70 |
Debian Forky/Sid lists fixes for 17 of the 22 issues, including SQLite 3.54.1, Expat 2.10.0, util-linux 2.42.3/2.42.4 and ACL 2.4.0. It leaves the three Git issues and both OpenSSL issues open. Testing or unstable packages mixed into Trixie would change the runtime and support model. Use fixed Trixie packages or signed, supportable backports when available. A different maintainable base needs a separate digest scan and a wheel/native-library compatibility, coordinated restore and retraining check. Reverting to Bookworm would reopen the previously observed critical OS findings.

Keep retraining disabled while its Git and XML risks are reviewed. Before removing Git, supply equivalent verified checkout provenance for training. Check whether MLflow can migrate to a supported PostgreSQL backend as a separate tested change with alias and artifact parity. That migration alone would leave the OS package installed. For release, verify per-digest executable and library ownership with `dpkg-query -L`, inspect runtime subprocesses and listeners, query SQLite compile options and schemas, and have the designated release/security owner review the evidence and any expiry-bound residual acceptance. Otherwise keep the production gate blocked.

After any image change, repeat the unfiltered digest scan, full hosted CI, and the targeted coordinated restore, Kafka continuity and Feast byte parity checks. Bind the resulting evidence to the deployable manifests and verify the registry digests after publication. [Hosted regression at f1c619e](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45597573046) [Kafka compatibility](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45591965673) [Feast byte parity](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45594638604) [Coordinated patched-stack recovery](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45597573128)
