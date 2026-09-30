# PROPOSED Phase 2A VM configuration and change plan

This is a project design, not provisioning authorization or evidence of isolation.
The human-approved memory amendment is a 4 GiB total guest envelope with a 512 MiB
fixture process-tree cap inside it. Code-only contracts and simulation persistence/
recovery are implemented separately; they cannot approve or launch a VM. Other
resource limits, timeouts and research parameters below remain PROPOSED.

Private laptop inspection reports, account identifiers, machine inventory and
conversation transcripts are intentionally excluded from this repository. Recheck
operational prerequisites, resource headroom and pending maintenance before any
separately approved provisioning. Existing host virtualization settings and other
applications must not be changed as an incidental part of lab setup.

## Proposed configuration

| Component | Configuration |
| --- | --- |
| Host/VM | Hyper-V Generation 2; one disposable VM at a time; no nested virtualization |
| Guest | Ubuntu Server 24.04.5 LTS AMD64; Linux Secure Boot using Microsoft UEFI Certificate Authority template |
| CPU | PROPOSED one vCPU; one fixture process group at a time |
| Memory | APPROVED 4 GiB total guest / 512 MiB fixture split; static allocation and zero guest/fixture swap are proposed enforcement settings |
| Fixture controls | PROPOSED 120 s deadline, 32 tasks, 64 MiB scratch charged within the 512 MiB cgroup limit |
| Other deadlines | PROPOSED 300 s boot deadline and 10 s host escalation allowance after fixture timeout |
| Disk | PROPOSED fixed 32 GiB base VHDX plus one full fixed 32 GiB disposable copy; no automatic expansion or checkpoint/differencing chains |
| Networking | No virtual NIC or switch attachment; no SSH/RDP |
| Host sharing | No shared drives/folders, clipboard, enhanced-session redirection or device passthrough |
| Inputs | Sealed read-only ISO, exact approved hashes, fixed filenames; PROPOSED 64 MiB image cap |
| Outputs | Proposed single bounded Hyper-V socket receiver bound to exact VM ID, with no guest management interface |
| Output budgets | PROPOSED 64 KiB frames, 1 MiB stdout, 1 MiB stderr, 64 KiB metrics, 4 MiB total wire bytes/run |
| Lifecycle | No automatic start, saved-state restore or checkpoints; fresh run disk per authorization |
| Evidence | PROPOSED 1 GiB retained evidence quota; refuse admission before exhaustion; no automatic failure deletion |

The original PROPOSED 512 MiB whole-environment limit is **not satisfied by a
4 GiB VM**. The approved amendment separates fixture accounting from infrastructure;
formal research registration must record it before affected research. The guest
budget is 4 GiB total, not 4 GiB plus another 512 MiB. Ubuntu documents a 1.5 GB
ISO-install minimum and suggests at least 3 GB for this release; a strict 512 MiB
whole-guest requirement would need a different reviewed design.
[Ubuntu requirements](https://ubuntu.com/server/docs/reference/installation/system-requirements/)

PROPOSED planning allowance: 5-6 GiB additional host RAM, including the 4 GiB VM;
require 14 GiB available before start and retain at least 8 GiB host headroom.
Reserve approximately 80 GiB on the lab data volume: 64 GiB logical disks plus
media, VHDX/runtime metadata, evidence and margin. Require 100 GiB free on the
data volume and 20 GiB free on the system volume before relevant setup. These
are admission estimates, not measured overhead or OS storage quotas.

Example proposed storage root: `D:\Prometheus-Lab-VM`, outside the source checkout,
with separate `media`, `base`, `vm`, `inputs`, `runs`, `control`, and `evidence`
directories. Resolve/review exact paths and reject reparse-point escapes before
creation. Never overwrite existing resources to make these names available.

## Official media and verification

Pinned server filename: `ubuntu-24.04.5-live-server-amd64.iso`.

- [Official index](https://releases.ubuntu.com/24.04/)
- [Official ISO](https://releases.ubuntu.com/24.04/ubuntu-24.04.5-live-server-amd64.iso)
- [Checksum manifest](https://releases.ubuntu.com/24.04/SHA256SUMS)
- [Detached signature](https://releases.ubuntu.com/24.04/SHA256SUMS.gpg)
- [Canonical verification procedure](https://ubuntu.com/tutorials/how-to-verify-ubuntu)

Published SHA-256 observed during design preparation:
`97f3d7ffb032c3eb3b23d2c8be9cc76e60c2c1f2c0146ba5ba9fe01cafae0fd8`.
Canonical's documented CD-image signing key fingerprint:
`8439 38DF 228D 22F7 B374 2BC0 D94A A3F0 EFE2 1092`.

After separate acquisition approval, verify the signature over the exact checksum
manifest using the full verified public-key fingerprint, then verify the ISO's
SHA-256 and exact filename. Archive the verification transcript. A changed key,
checksum or missing pinned release stops acquisition for review; no silent version
substitution. No local ISO or signature has been cryptographically validated by
this planning document. An ISO-building utility or missing offline dependency
also requires explicit review; never enable networking to repair an offline guest.

Microsoft documents [Ubuntu 24.04 support](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/supported-ubuntu-virtual-machines-on-hyper-v),
[host prerequisites](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/host-hardware-requirements),
and [Generation 2/Linux Secure Boot](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/plan/Should-I-create-a-generation-1-or-2-virtual-machine-in-Hyper-V).

## Trust and evidence boundaries

The proposed trusted computing base includes host OS/hypervisor, external control
services, guest kernel and a small guest supervisor. Fixture processes and all
their output are untrusted. The proposed fixture UID has no login/sudo/capabilities,
no writable cgroups, only bounded scratch and a restricted runtime/input view.
No fixture-accessible management sockets or privileged inherited descriptors.
Effective resource/device/socket restrictions require later OS-level tests.

Real approval needs a separate authenticated human interface, with exact manifest,
identity, operation, expiry and one-use bindings. The code-only `MockIdentity`
and in-process validation cannot implement that authority. Proposed broker and
watchdog identities need a reviewed privilege manifest; broad Hyper-V management
rights are not equivalent to OS-enforced per-VM rights. Never silently use an
administrator account or LocalSystem as a fallback.

The proposed guest supervisor alone owns one Hyper-V socket endpoint; its host
collector has no VM-control or approval rights. Frames are bounded before memory
allocation; output is inert data with host-generated filenames. No archives,
executable deserialization, guest-selected host paths or guest-filesystem mounts.
This transport works without TCP/IP but is bidirectional and is an attack surface,
not a data diode. [Microsoft Hyper-V sockets](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/make-integration-service)

The future external controller must preserve failures, reconcile host lifecycle
observations with guest claims, and refuse completion after missing/contradictory
evidence or an unsuccessful stop. An independent watchdog and durable evidence
initialization must be ready before launch. Local SQLite simulation persistence
does not provide those external controls, independent provenance, hostile rollback
resistance or tested power-loss guarantees. See [durable simulation](DURABLE_SIMULATION.md).

## Phases, approvals and stopping points

1. Read-only prerequisite review: optional features, running management service,
   successful `Get-VMHost`/`Get-VM`/`Get-VMSwitch`, hypervisor/firmware prerequisites,
   available RAM/disk and pending reboot indicators. Stop on unresolved prerequisites.
   An active hypervisor can suppress bare-metal CPU requirement reporting.
2. Code-only simulation contracts and persistence: review and unit/crash tests;
   the unavailable VM boundary stays unconditional. Stop for human review.
3. Separately approve exact downloads, offline packages/utilities, feature changes
   if actually required, any reboot, paths/disks, accounts, and offline setup boots.
   Acceptance: verified media, zero NICs/shares/redirection, expected config and
   hashes, complete offline dependency inventory. Stop with a sealed powered-off base.
4. Separately approve external identity, broker/watchdog/collector implementation,
   service identities, ACLs, endpoint registration, retention and rollback manifest.
   Acceptance: missing/expired/replayed approval or unavailable evidence/watchdog
   prevents a start. Stop before guest fixture authorization.
5. Separately approve a bounded offline fixture suite and per-run manifests.
   Corroborate denial using host lifecycle records; resource limits using effective
   kernel controls/counters; write isolation using external canary hashes; and
   failure handling using malformed output, controller loss and stop faults.
   Preserve failures, report passed/failed/not-run and stop. No automatic research.

Feature installation, if needed, is distinct from read-only inspection. Reboot
and feature removal require separate decisions; do not alter unrelated host VMs,
switches, WSL or Docker. Rollback affects only reviewed newly created resources
after preserving evidence. Forced shutdown can corrupt the disposable disk and
lose memory. No snapshot or uninstall can undo a host compromise.

Passing functional fixtures would not prove containment against hypervisor/device
exploits, malicious host administrators, compromised trusted components, all side
channels or host-wide failure. No phase here establishes research improvement.
Keep other sample sizes, budgets and thresholds PROPOSED; preserve design
registration before search, candidate lock before final evaluation, protected
evaluation without feedback to improvement, external authority and separate
promotion approval.
