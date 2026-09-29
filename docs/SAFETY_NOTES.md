# Safety notes

**Research only. Not for production or safety-critical use.**

**The MVP sandbox is conceptual, not a real security boundary.** It does not
isolate a process, filesystem, network, credentials, or operating-system user.
Its only execution behavior is constructing a `simulated` result. It never
applies the proposed replacement text or executes it. The toy target is unchanged.

## What is enforced in the supported loop

- Proposals follow a typed schema with no additional fields.
- Default checks require simple relative `.py` paths beneath the toy namespace,
  unique paths, bounded file count, and bounded UTF-8 content size.
- Any failed check prevents approval and execution. Check exceptions fail closed.
- A simulated denial prevents execution. Approval binds the entire proposal
  digest; a mismatch raises an error.
- Prior-stage audit writes must succeed before progression. An unavailable audit
  store prevents execution if it fails before executor invocation.
- Results explicitly state that no files changed and no improvement was measured.

## What is not established

There is no real human-in-the-loop review. The default decision is an automatic
simulation, clearly labeled in the CLI and audit. It must never authorize a
future real executor. The digest detects accidental proposal/decision mismatch;
it is not a signature, identity check, or authorization credential.

Path checks are lexical, not filesystem containment. They do not resolve
symlinks, junctions, mount points, races, or actual target existence. Content is
not analyzed for malicious behavior or semantic correctness. The in-process
agent is trusted Python code: it could bypass these conventions and use all
permissions available to the process. Python immutability and module separation
do not isolate hostile agents.

JSONL is append-only **through this API**, not immutable or tamper-proof storage.
Anyone with filesystem access can rewrite it. There are no signatures, hash
chains, locks, recovery guarantees, or corruption detection. Use one writer per
log; a crash or I/O error can leave a partial record. Preserve raw failures and
inspect incomplete runs before relying on the evidence. If the audit destination
is unwritable, its own failure may be visible only on stderr/stdout. There is no
transaction that atomically couples an external side effect to audit persistence.

Audit payloads include full proposed source text and error messages. This MVP
does not redact sensitive data. Use only non-sensitive toy inputs and trusted
audit paths. Disk use grows with every run; retention is a manual research
decision, not silent deletion by the application.

## Before any real execution

Separately review the threat model, independent human authorization, isolation,
policy ownership, audit integrity, recovery, and evaluation/promotion workflow.
Keep agent-editable files separate from trusted constraints, orchestration,
review records, and audit infrastructure. Builder != Reviewer != Authority.
Capability does not create authority; this scaffold is not a safety proof.
