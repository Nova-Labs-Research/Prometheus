# PROPOSED external observation and recovery design

Status: engineering proposal only, 2026-09-30. No identity, service, permission,
transport, credential, live adapter or guest change is implemented by this document.
It does not authorize activation, prove isolation or establish research improvement.

## Recommendation and inspected baseline

Build the next **code-only observation/recovery contract** first. Keep the live VM
boundary unavailable. Later, prefer local OS-authenticated, separately identified
host reporters over signed JSON supplied by callers. Treat guest output as guest
claims even when the transport identifies the correct VM. Authentication identifies
an endpoint; it does not establish that a report is true or that a human intended it.

The inspected checkout remains `controller-durable-preflight` at
`0b8ff20897ca039332f8d50ce6cb32b500cabaac`. Its committed tree matches verified
main merge `5f7b2f5133d1de4e0fdf97ac27121e796f4c6d90`
(tree `e036f93d93a34c8b0294e86b5374c97ec7f256e3`). No checkout/fetch/pull is
needed to prepare this plan. Existing source and tests remain unchanged.

Read-only capability inspection found PowerShell, Hyper-V management commands,
a running VM management service, .NET SDK and an NTFS lab volume. The current
normal-user token could not query the lab VM; no UAC elevation was requested.
Thus current VM configuration, guest kernel/cgroup/socket support and usable
least-privilege Hyper-V access are **not newly verified**. Installation and shutdown
remain user reports. No personal account identifiers, machine inventory, actual VM
IDs or private inspection logs belong in this public document.

Relevant existing code:

- `src/prometheus_lab/lab_control/contracts.py`: `MockApproval`, caller clock and
  manifest whose only operation is `validate_fixture_contract`, execution unavailable.
- `controller.py`: immutable requests, bounded parsing and mock readiness checks.
- `controller_journal.py`: version 2 preparation consumes IDs, retains unresolved
  intent, accepts only `TestObservation(source="test_only")`, and permits only
  current-head `abandon_unverified`. Every result remains non-authorizing.
- `boundary.py`: unconditional `VMOperationsUnavailable`; no backend or enable flag.
- `tests/test_controller_journal.py`: crash/reopen, replay, binding, corruption and
  observation/reconciliation races. These establish local contract behavior only.

Do not reinterpret a v2 record, relabel a mock identity, or add `authenticated=true`
to existing input. A future external evidence schema/store must be separate, reject
v1/v2 as authorization, and retain their records as historical simulation evidence.
See [journal](CONTROLLER_JOURNAL.md), [controller](CONTROLLER_REHEARSAL.md) and
[VM plan](PHASE2A_VM_PLAN_PROPOSED.md).

## Trust and authority model

Trusted: host kernel/hypervisor, approved service binaries and protected deployment,
installer/host administrator, journal owner, and explicitly selected OS identity
adapter. Guest kernel and supervisor are trusted only for guest measurements;
a compromised guest can fabricate its own measurements. Untrusted: fixture/candidate,
builder-produced manifests, incoming bytes, caller identity labels and guest output.

Separate decisions are required for (1) human approval of an exact operation,
(2) broker admission/dispatch, (3) observation of what happened, (4) evaluation of
results, and (5) promotion. No reporter may approve, no approval grants evaluation
access, and neither a launch acknowledgment nor VM-off report is a successful
fixture/evaluation verdict. Builder access excludes approver identity, control
storage, service configuration and protected evaluation data. If builder and
approver share a Windows token, this separation has NOT been achieved.

All services remain in one host trust domain: separation helps against an
unprivileged builder/guest, not a malicious administrator, compromised host kernel,
hypervisor exploit or colluding trusted services. Local ACLs/hash chains are not
independent provenance, a remote attestation system or malicious rollback protection.
No independent clock or off-host evidence anchor is proposed as already available.

## Proposed identities and minimum privilege contract

The following names are reserved design choices, not installed accounts. Numeric
SIDs, service binary hashes, final DACLs and one allowed VM ID must be resolved in a
private deployment manifest and approved before activation. A placeholder blocks
installation; do not guess identity from a display name or reuse an existing name.

| Role / proposed identity | Required access | Explicit exclusions |
| --- | --- | --- |
| Human: dedicated local standard account `PrometheusApprover` | Interactive trusted approval UI; decision pipe; read frozen public manifest and evidence summary | No builder session/token; no service/config/database writes; no Hyper-V control |
| `NT SERVICE\PrometheusGate` | Own admission/evidence journal; receive authenticated peer messages; issue one-use dispatch decision | No Hyper-V rights, guest channel or arbitrary process launching |
| `NT SERVICE\PrometheusObserver` | Query approved VM configuration, lifecycle and relevant job state through local provider; own bounded report spool | No start/stop/configuration, approver or journal write rights |
| `NT SERVICE\PrometheusBroker` | Query and request start of exactly the approved VM; own durable dispatch fence/receipt spool | No provisioning, clone, switch, checkpoint, disk attachment, arbitrary WMI method or command execution |
| `NT SERVICE\PrometheusWatchdog` | Independently query and enforce approved stop policy for that VM; own lease/stop journal | No start, approval, guest-output parsing or permission to clear unknown state |
| `NT SERVICE\PrometheusCollector` | One approved Hyper-V socket endpoint; own bounded guest-data spool; send collector receipts to Gate | No VM control, host observer claims, approval or journal modification |
| Guest `prometheus-supervisor` service and `prometheus-fixture` UID | Supervisor owns measurement/channel/cgroup setup; fixture receives only approved read-only inputs and bounded scratch | Fixture has no login/sudo/capabilities, writable cgroups, management endpoint or inherited supervisor descriptors |
| Evaluation reviewer | Read sealed exports only, through a separate reviewed evaluation workflow | No approval/launch rights granted by this design; no final-test feedback to builder |

Five separate host service processes are the proposed minimum for this separation.
Gate combines approval checking and journaling, so compromise of Gate compromises
both; that accepted trust concentration must be explicit. Use virtual service
accounts, not shared LocalSystem/administrator credentials; Microsoft documents
service SIDs as ACL principals and managed virtual accounts. Their presence does
not automatically create isolation or prevent network access.
[Service SIDs](https://learn.microsoft.com/en-us/windows/win32/api/winsvc/ns-winsvc-service_sid_info),
[service accounts](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-service-accounts).

**Blocking permission question:** required per-VM/per-operation rights above are
requirements, not verified Hyper-V grants. `Grant-VMConnectAccess` grants console
connection; it does not document this start-only/stop-only split. WMI namespace
read/method rights do not prove per-instance enforcement. No supported least-
privilege recipe has been demonstrated on this laptop. Never substitute broad
Hyper-V Administrators membership or LocalSystem. Keep live adapters disabled
until denial against other VMs and prohibited methods is demonstrated, or present
a separately approved architecture change accepting a broader broker trust domain.
[Console access](https://learn.microsoft.com/en-us/powershell/module/hyper-v/grant-vmconnectaccess?view=windowsserver2025-ps),
[WMI rights](https://learn.microsoft.com/en-us/windows/win32/wmisdk/access-to-wmi-namespaces).

Recommended option: prove scoped access before live implementation. Alternative:
an administrator-controlled broker with broader underlying Hyper-V rights and
application-level allowlists is simpler but increases blast radius; it is NOT
OS-enforced per-VM authority and is not the approved baseline. Manual observation
exports are useful for human inspection but cannot unlock automated execution.

## Authentication, channels and evidence origin

Propose local named pipes between host services, using explicit DACLs and remote-
client rejection. Gate owns `\\.\pipe\Prometheus.Phase2A.approval.v1` (Approver only)
and `...evidence.v1` (Observer/Broker/Watchdog/Collector only). Broker owns
`...dispatch.v1` (Gate only); Watchdog owns `...lease.v1` (Gate only). Responses
travel over the same authenticated connection. No TCP listener, WinRM endpoint,
firewall opening or guest NIC is required by this proposal.

Only endpoint owners may create server instances. Specify individual client
read/write rights, excluding `FILE_CREATE_PIPE_INSTANCE`, ACL writes and ownership
changes; do not grant client `FILE_GENERIC_WRITE` blindly. Refuse pipe-name
collisions using first-instance protection; fail closed on ambiguous ownership.
[Pipe ACLs](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights),
[creation and remote rejection](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createnamedpipea).

For each bounded request, the server obtains the OS peer token associated with that
connection/message, verifies the exact allowlisted user/service SID and session,
and restores its own identity in all paths. Identification-level client tokens
are preferred; no new `SeImpersonatePrivilege` grant is planned. Failed identity
lookup or reversion terminates admission; never continue as the service identity.
Clients must also verify the server against SCM identity, process handle/token
and protected binary deployment; a PID or pipe name alone is insufficient, including
PID reuse. The precise native/.NET wrapper and required token-query rights remain
unverified until separately approved OS integration tests. Do not use remote/local
JSON SID strings as proof. [Impersonation failure behavior](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-impersonatenamedpipeclient),
[server PID API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeserverprocessid).

An authenticated Approver process is not proof of human presence. Use a personally
controlled separate Windows logon, approved UI displaying the frozen manifest,
explicit confirm/deny and immutable decision receipt. No agent automation may
enter that session or submit consent. Same-account malware/UI spoofing and a
compromised host remain outside this claim. No password goes into chat, code,
config, receipts or logs; account creation/credentials require a later personal step.

No signing keys/certificates or shared secret are needed for this local-channel
proposal. Journal records contain Gate-attested peer identity obtained at ingress,
not portable cryptographic signatures by each reporter. A copied export alone
therefore cannot prove origin to an independent third party. Independent signed
receipts or an off-host anchor would require a separate key custody/revocation plan.

Hyper-V sockets are a distinct guest channel, not the host identity mechanism.
Microsoft documents non-IP host/guest streams, Linux kernel requirements and the
Linux port-derived ServiceId template. Use one collision-checked port `P` and
`{P-as-8-hex}-facb-11e6-bd58-64006a7986d3`, resolved in the deployment manifest;
no arbitrary random Linux ServiceId. Register only that entry under
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices`.
Verify exact peer VM from OS endpoint metadata, not the frame. Non-wildcard binding,
endpoint access restrictions and peer lookup behavior on this build are activation
tests, not established guarantees. [Microsoft socket documentation](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/make-integration-service).

A VM endpoint identifies a partition, not the supervisor process. Guest channel
messages remain `guest_claim`; even a successful challenge does not authenticate
the supervisor against a compromised guest kernel. Cgroup reports are guest-measured under a trusted guest kernel/supervisor assumption,
correlated with the approved baseline and separately approved actual tests. Host
VM identity/lifecycle evidence cannot certify guest cgroup truth or independently
attest that baseline.
Use bounded, length-prefixed inert data, host-chosen filenames, no archive extraction,
mounting guest filesystems, pickle/eval or guest-selected paths. Do not expose shell
commands or management RPC over this channel. A narrow host challenge is bidirectional
traffic, not a data diode or a shared host filesystem.

## Binding and freshness contract

A proposed future immutable manifest extends, rather than changes the meaning of,
the unavailable code manifest. Bind all of: schema/canonicalization version;
installation ID; host-control epoch; store ID; run/attempt/approval/operation IDs;
exact VM ID and disposable disk identity/initial prelaunch hash; design, candidate/fixture, runtime,
base, input, policy and effective isolation hashes; service deployment digest;
limits; evidence schema; approval interval; stop authority and recovery policy.
Recreation/import of a VM, substituted disk, changed resource setting, policy or
service build requires a new manifest and approval. A display name or VM ID alone
is insufficient. Sealed base/input hashes remain fixed; expected guest writes to
the disposable disk do not invalidate its initial hash binding. Runtime/post-stop
disk hashes are separate observations, not required to equal the initial hash.
The future operation set is fixed reviewed fixture lifecycle verbs, never an
arbitrary command or authorization to execute agent candidates.

Each observation binds the above manifest digest plus explicit VM, attempt,
approval and operation IDs; issuer role; reporter instance; observation ID; per-
issuer sequence; query/challenge ID; collector receive sequence; query start/end
host monotonic ticks; received tick; wall time for audit; payload hash/length;
raw provider result/job reference or guest claim; uncertainty and sampling interval.
Gate adds authenticated transport provenance from outside the payload. Reject
unknown fields, duplicate keys, malformed encoding, role/type mismatch, substituted
identity, changed context and unsupported versions. Immutable normalized inputs
must match the retained original digest; never silently repair records.

Fresh query challenges come from Gate, are durably associated with one epoch/
attempt and expire once answered or timed out. Reusing an observation ID/sequence
with identical bytes is a duplicate with no second state transition; conflicting
bytes are contradictory evidence and quarantine the run. A lost acknowledgment
is handled by querying the retained receipt, not sending another effect. A fresh
challenge means freshly requested evidence, not an atomic or complete state sample.

Use trusted host monotonic intervals, not guest wall timestamps, for admission
freshness/deadlines. Freshness window and approval TTL must be explicit PROPOSED
policy values approved before activation; absent values deny. Any Gate/reporter/
broker restart, host reboot, suspend/resume or clock anomaly invalidates outstanding
launch approval/challenges and requires reconciliation plus a fresh human decision.
Never compare guest/host ticks or different epochs. QPC is not a UTC authority;
no cross-reboot monotonic continuity or clock-tamper resistance is claimed.
[Windows timing](https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps).

Ordered `off -> running -> off` samples can be normal lifecycle transitions.
Conflicting claims about the same query/interval, unexplained transitions, missing
sequence segments or mismatched configuration cannot be reconciled by last-write-
wins. This differs deliberately from v2's simple off/running conflict indicator;
do not retrofit historical test observations as an authenticated timeline.

## Launch uncertainty and recovery policy (future live design)

Preparation and an external Hyper-V action cannot share an atomic SQLite transaction.
The target is **at most one dispatch attempt with explicit uncertainty**, not exactly-
once execution. No automatic retry of a launch is ever inferred from silence.

1. Gate freezes manifest and authenticated consent; commits approval consumption,
   intent and report references before dispatch. Readback failure stops admission.
2. Watchdog durably arms a VM/attempt-bound stop lease and acknowledges readiness;
   evidence capacity is reserved. Missing/expired readiness blocks dispatch.
3. Broker independently validates Gate identity, manifest, current config, off state,
   fresh epoch and one-use ID. It commits its own dispatch fence, then immediately
   before its sole provider invocation rechecks approval expiry, deployment/config,
   epoch and the exact live Watchdog lease. Delayed requests past expiry never
   dispatch; cancellation after the fence retains consumption and a failed intent.
   No shell or generic executable/method argument is accepted. This last check and
   the provider call are not atomic: scheduling pauses or provider latency can
   cross the deadline. No hard real-time or no-start-after-expiry guarantee is
   claimed; unresolved dispatch must remain covered by recovery/stop authority.
4. Record provider acknowledgment/job identity and fresh external lifecycle samples.
   Method success is not fixture completion. An asynchronous job stays pending.
5. Seal bounded evidence only after required host lifecycle and guest measurement
   evidence is complete and noncontradictory. Report evidence consistency separately
   from measured fixture outcome and later evaluation/promotion decisions.

Microsoft documents asynchronous `RequestStateChange` and warns that repeated calls
can overwrite/lose earlier requests. Its timeout parameter is unused. A host-side
watchdog/timeout remains necessary; returning from an API is not an independent
observation. [Provider method](https://learn.microsoft.com/en-us/windows/win32/hyperv_v2/requeststatechange-msvm-computersystem).

| Failure / observed state | Required retained state and allowed next action |
| --- | --- |
| Rejected before intent commit | Durable rejection if recording succeeds; otherwise recording failure, never a recorded-success claim |
| Intent/fence committed, crash before or after provider call | `dispatch_unknown`; approval remains consumed; inspect journal, broker instance and provider jobs; no redispatch |
| Running reported, receipt missing | `running_unattributed`; preserve both facts, use only previously approved stop policy, block new launch |
| Off observed after uncertain dispatch | `off_observed_history_incomplete`; does not establish that nothing ran or that a queued start cannot run later |
| Pending/unknown provider job, old broker still alive, or query failure | Quarantine VM; do not dispose/reuse disk or clear launch fence |
| Reporter/Gate/watchdog failure, evidence full/corrupt, expired lease | Block new starts; independent watchdog enforces its already-approved stop policy where possible; record missing evidence |
| Guest says finished, host still running or reports conflict | Incomplete/contradictory; never valid completion; no promotion |
| Stop request lost or VM state unknown | `stop_unknown`; retain stop attempt and query; escalate to human, no unbounded stop retries |
| Recovery with complete fresh off evidence and settled provider work | Human current-head decision may abandon failed attempt and admit a new approval; never convert it to success |

Reconciliation requires fencing the previous broker instance and proving no pending
provider operation can later start the VM, as well as a new off/config query.
Watchdog expiry forbids new dispatch; it does not retire monitoring or the
preapproved protective stop authority for an unresolved dispatch. Even an off
sample at expiry keeps the VM quarantined until the old broker and provider work
are settled. A later running sample invokes only that retained stop policy.
Approval expiry, dispatch cutoff and recovery-stop authority are distinct bindings
that the human must approve; no indefinite new launch permission is implied.
Changing a database epoch does not cancel an in-flight provider request. If the
job is unidentifiable or quiescence cannot be established, remain quarantined for
separately approved manual recovery. A later off sample alone never clears this.

A preapproved stop policy must distinguish guest graceful shutdown from host power-
off and authorize exact escalation limits. Forced power-off risks disk corruption
and loss of volatile evidence. The existing PROPOSED boot/fixture/escalation budgets
are unchanged. An emergency watchdog stop must not depend on a healthy Gate or
main evidence store; keep a bounded independent stop journal/reserve. If recording
also fails, attempt only the already-armed stop policy, preserve failure/unknown
status, and require human intervention. Host crash/suspend/provider failure may
prevent timely stopping; software on that same host cannot guarantee otherwise.

## Future change manifest, storage and reversibility

All entries below require separate explicit approval; no installation script is
provided. Resolve exact service hashes, SIDs, endpoint GUID/port, VM/disk identities,
ACL diff and resource/deadline values in a private activation manifest first.

| Object | PROPOSED exact scope / access intent |
| --- | --- |
| Host accounts | One separate human approver account; five named virtual service accounts above. No shared password, domain enrollment or broad group membership |
| SCM | Five own-process services with explicit executable paths/arguments; demand start initially, no automatic restart into execution; administrator-only service configuration/start/stop management |
| Service token policy | Service logon only as required; restricted service SID where tested compatible; no debug, backup/restore, take-ownership or new impersonation grants; inventory effective token rights before activation |
| `D:\Prometheus-Lab-VM\control\bin` and `control\config` | Administrator-owned immutable deployment/config; services read/execute only; builder and Approver cannot write. Quote absolute paths; no user-writable DLL/module search directories |
| `control\gate`, `control\broker`, `control\watchdog` | Only respective service modifies its own DB plus SQLite sidecars/directory; admin recovery access; other services use authenticated messages, not DB sharing |
| `evidence\observer`, `evidence\collector` | Respective reporter owns bounded spool. Other reporters cannot write. Gate receives bounded content over IPC, never follows incoming filenames |
| `evidence\sealed` | Gate-only writer, reviewer read-only export; no automatic deletion. Administrators remain trusted, not excluded by an authenticity claim |
| `base`, `inputs`, `runs`, `vm` | No new broad service write access. Future provisioning remains a separate admin operation; required Hyper-V worker access to run disk must be inspected/preserved, not guessed |
| Named pipes and guest endpoint | Only exact endpoints/peer allowlists above; one new GuestCommunicationServices entry with reviewed permissions, never overwrite an existing registration |
| Hyper-V/WMI | Exact query/start/stop grants only if proven enforceable. No global provider ACL reset, remote enable, switch/NIC or host Hyper-V-default change |
| Guest | Separate approved offline setup: supervisor unit, fixture UID, cgroup controls, input/channel restrictions. No SSH, networking, shares, clipboard or enhanced-session integration |

NTFS Modify rights for SQLite owners necessarily allow replacement/deletion; call
this trusted-owner persistence, not append-only storage. Validate handles/final paths
and reject reparse/hardlink escapes and writable ancestors before admission. Explicit
DACLs must include directory/sidecar behavior; inherited broad Users permissions are
not acceptable. Do not apply recursive ACL changes to existing lab resources.

A blob is bounded and hashed during ingestion, flushed and atomically sealed under
a host-chosen name, then referenced by a committed event. Missing blob after event
commit is incomplete evidence; an orphan blob is retained/quarantined, not invented
as a result. Crash between blob and event commits is not a distributed transaction.
Gate-owned records capture channel identity at ingestion; a reporter spool file or
self-reported hash cannot authenticate a later import. No automatic import repairs.

Reserve the existing PROPOSED 1 GiB evidence allowance for control metadata, failure
records, guest data and emergency stop reserve together; approve its partition and
bounded event counts before activation. Do not give each service a separate 1 GiB
budget. Full store stops admission; no deletion to manufacture space. Process-exit
fault tests do not establish storage/power-loss durability or hostile rollback
resistance; an older internally consistent store remains a documented threat.

Rollback: first inhibit new admissions, establish actual off state and settled
provider work, then manually stop/remove only the approved new services/endpoints
and restore recorded ACL additions while preserving evidence. Never remove existing
VM resources or clear unknown state as cleanup. Account deletion/recreation changes
identity and requires a new deployment manifest. Service/token configuration may
require restart/reboot; resolve that during activation planning, never reboot
implicitly. Rollback cannot undo a compromised host or reconstruct lost evidence.

## Resource and research invariants

Retain offline/no-NIC architecture, 4 GiB total fixed guest allocation and the
approved **512 MiB fixture process-tree cap inside that allocation**. The latter
requires effective guest cgroup enforcement; VM RAM alone does not implement it.
Propose cgroup v2 memory.max=536870912 and memory.swap.max=0 for the full fixture
subtree, with supervisor outside it, plus the existing PROPOSED task/scratch/time
limits. Inspect ancestor limits, OOM events and effective settings; disallow fixture
migration out of the subtree. Swap and controller availability remain unverified.
[Kernel cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html).

Infrastructure overhead remains separate from fixture accounting. Preserve all other
PROPOSED sample sizes, budgets and thresholds from the VM plan; no new experimental
settings become approved here. Record the approved memory amendment in formal
preregistration before research. Keep design preregistration before search,
candidate lock before final evaluation, protected evaluation without feedback to
improvement, external human authority and a separate promotion decision.

## Acceptance evidence and next bounded milestone

| Test family | Required outcome / evidence | Stage |
| --- | --- | --- |
| Schema and binding | Mutate each VM/attempt/approval/manifest/deployment field alone from a positive baseline; reject without transition | Next code-only slice |
| Trust separation | Test-only peer context and fabricated auth flags never become authoritative; guest completion cannot unlock launch/evaluation | Next code-only slice |
| Time/replay | Duplicate identical receipt causes no transition; conflicting duplicate, stale challenge, changed epoch, exact expiry and suspend/restart block | Next code-only slice |
| Lifecycle ordering | Legitimate ordered transitions differ from same-query contradiction; dropped sequence and absent/partial evidence remain incomplete | Next code-only slice |
| Recovery | Inject faults before/after intent, fence, acknowledgment, report and reconciliation; one conceptual dispatch, no blind retry; off plus pending job stays quarantined; delayed dispatch after consent/lease expiry cannot retire watchdog protection | Next code-only slice |
| Durable recovery | Reopen temporary stores, lost acknowledgments, concurrent observation/head-bound decisions and corrupt/unknown versions preserve uncertainty | Next code-only slice |
| Identity/IPC | Real distinct OS tokens, pipe squatting/PID reuse, forged SID, remote client, failed impersonation/reversion and wrong role denied; verify effective ACLs | Later separately approved host integration |
| Scoped authority | Other VM and prohibited methods denied by OS boundary; collector/approver cannot manage VM; broker cannot provision or alter configuration | Mandatory before live activation |
| Evidence storage | Reporter cannot alter another spool/journal; partial blobs, sidecars, reparse races, quota exhaustion and power interruption measured separately | Later approved OS/storage tests |
| Guest channel/resources | Wrong VM/process claims cannot satisfy host evidence; framing/flood bounds; actual cgroup memory/tasks/scratch and no escape measurements | Later approved offline fixtures |
| Real recovery | Controller/watchdog loss, delayed provider job, stop uncertainty, suspend/reboot and evidence failure; preserve all failures | Later approved lifecycle tests |

Next implementation proposal: add a separate strict observation envelope, provenance
categories and pure recovery reducer with a temporary-store test harness. Keep all
injected peers explicitly test-only and all production authentication adapters
unsupported. Use explicit interfaces for verifier-produced peer context without
accepting serialized authentication booleans. Test the table's first six rows,
including fake provider timelines; these are software-contract evidence, never
actual lifecycle observations. Preserve existing v1/v2 schema/API semantics and
unconditional unavailable VM boundary. No service, IPC endpoint, installer, adapter,
credential or change to existing research parameters belongs in that milestone.
Run full configured local tests and obtain separate review; stop for human review.

Individual decisions before further action:

1. Approve that bounded code-only milestone and its new schema/store separation.
2. Approve the proposed dedicated human identity and five service roles, acknowledging
   same-host trust and lack of portable third-party provenance.
3. Resolve/prove least-privilege Hyper-V scope, or explicitly review a changed trust
   model; no live permission grant follows merely from accepting this document.
4. Separately approve fully resolved deployment/ACL/SCM/endpoint/guest manifest,
   exact deadline/freshness/retention partitions, stop escalation and recovery policy.
5. Only after OS acceptance evidence, separately authorize bounded offline fixtures.
   Models, candidate code, research experiments and promotion remain later decisions.

## Review record

A separate read-only AI reviewer compared this proposal with the journal, contracts,
unavailable boundary and VM plan. Two medium findings were corrected: late dispatch
needed a final expiry/lease check plus retained watchdog protection through unresolved
provider work; cgroup reports had to be labeled guest-measured rather than host-
verified. A low-severity clarification separated initial run-disk hashing from
expected runtime writes. The reviewer re-read all corrections and the acceptance
table and reported no remaining substantive finding.

This was independent AI static critique, not independent test execution, human
approval or security certification. Neither agent ran code tests, fixtures or VM
operations for this documentation-only task. Relative document links and whitespace
checks passed; only this new proposal is changed. Source inspection and official
documentation establish design grounding, not deployment readiness. All activation
unknowns and future approval requirements remain open.
