# Offline Telemetry for Edge-Node Applications

Status: reference design  
Audience: application owners, platform engineers, security reviewers, and operators  
Applies to: interactive or batch applications deployed on shared Linux edge nodes

## 1. Purpose

This guide defines a reusable telemetry pattern for applications that run on
edge nodes with unreliable or prohibited network egress. It answers two basic
operator questions:

1. **Who uses the application?**
2. **How is it used?**

The design records low-volume product events in append-only local JSONL files.
It writes a private per-user copy and, when available, a shared fleet-readable
copy. Telemetry is always best-effort: storage failures, contention, malformed
inputs, or shutdown timing must never block or break the application.

This is a design guide, not a shared runtime library. Each application owns its
event catalog and integration points while preserving the safety and storage
contracts defined here.

## 2. Applicability and assumptions

Use this design when all of the following are true:

- The application runs as the logged-in OS user on a shared Linux edge node.
- SaaS analytics or outbound HTTP calls are unavailable, undesirable, or not
  approved.
- Product events are low volume: sessions and meaningful actions, not
  keystrokes, log lines, or high-frequency metrics.
- A trusted deployment account can create one shared application directory.
- Operators can read that shared directory locally or through existing edge
  node administration paths.

Reconsider this design if the application emits hundreds of events per second,
requires guaranteed delivery, must aggregate across many nodes in real time, or
handles identities that cannot safely be represented by the OS account.

## 3. Adaptation placeholders

Replace these values before implementation:

| Placeholder | Meaning | Example |
|---|---|---|
| `{APP_NAME}` | Human-readable application name | `Query Console` |
| `{APP_SLUG}` | Safe lowercase path component | `query-console` |
| `{APP_COMMAND}` | Installed CLI command | `query-console` |
| `{APP_HOME}` | Private per-user application directory | `~/.query-console` |
| `{SHARED_TELEMETRY_DIR}` | Trusted shared telemetry directory | `/ads_storage/query-console/telemetry` |
| `{ENV_PREFIX}` | Environment-variable prefix | `QUERY_CONSOLE` |
| `{APP_VERSION}` | Installed application version | `2.3.1` |

`{APP_SLUG}` must be exactly one path component. The resolved OS username is
stored as data, while its deterministic encoded filename token must satisfy the
single-component grammar in §9.3. Reject empty values, separators, traversal,
control characters, and overlong identities.

## 4. Goals

- Report active users, session volume, feature usage, successful actions,
  refusals, failures, and cancellations relevant to product decisions.
- Operate without network calls or new runtime services.
- Keep all filesystem work off the application's UI loop and critical action
  paths.
- Preserve user privacy through a small, explicit event catalog.
- Remain safe when the shared directory is world-writable for file creation.
- Produce files that operators can inspect with standard local tools.
- Degrade to private telemetry when the shared destination is unavailable.

## 5. Non-goals

- Guaranteed delivery or audit-grade accounting.
- Real-time dashboards, alerts, or cross-node aggregation.
- Capturing SQL, document contents, command output, email addresses, secrets,
  absolute user file paths, or arbitrary form payloads.
- Replacing operational logs, traces, security logs, or business records.
- Changing the lifecycle or durability semantics of the host application.

## 6. Chosen architecture

```text
application lifecycle and actions
              │
              ▼
typed event-specific helpers
              │
              ▼
bounded in-memory queue
              │
              ▼
serialized background consumer
              │
              ├── private JSONL: {APP_HOME}/telemetry/events.jsonl
              │
              └── shared JSONL:
                  {SHARED_TELEMETRY_DIR}/users/<encoded-user-token>.jsonl

operator
   │
   └── {APP_COMMAND} telemetry who|summary
```

### 6.1 Producer contract

Event helpers perform only bounded in-memory work:

1. Validate a catalogued event and its allowed property values.
2. Capture timestamp, OS username, session ID, and application version.
3. Serialize one size-limited JSON record.
4. Add it to a bounded data queue without waiting.

The producer must not create directories, open files, acquire file locks, scan
the filesystem, wait for the writer, or raise telemetry errors into its caller.
If the queue is full, drop the new event and emit a debug log.

Reserve two physical queue slots for shutdown control records: configure
physical capacity as `DATA_CAPACITY + 2`, but reject normal producer events
once `DATA_CAPACITY` records are queued. The reserved slots hold session-end
and the flush marker after producers are closed.

### 6.2 Writer contract

One lazily started serialized background consumer drains the queue in order. A
daemon thread is the reference mechanism for Python, but another runtime may
use an equivalent worker that cannot keep the process alive indefinitely. The
consumer performs all directory creation, safe file opening, locking,
appending, permission changes, and error logging.

The consumer attempts the private append and shared append independently.
Ordinary validation, open, lock, and write failure in one destination must not
suppress the other. A kernel-level uninterruptible filesystem stall can still
prevent the later attempt; that residual event-loss risk is accepted because
the application remains unaffected. Applications requiring destination-level
delivery isolation need separate bounded consumers and are outside this
baseline design.

### 6.3 Shutdown contract

Telemetry has an explicit state guarded by the same synchronization boundary
used to enqueue records:

```text
accepting ── begin shutdown ──► closing ── writer acknowledged ──► closed
```

- `accepting`: event helpers may enqueue.
- `closing`: new producer events are dropped; shutdown alone attempts to queue
  at most one session-end record followed by at most one flush marker.
- `closed`: all events are dropped and no writer is restarted.

On a normal shutdown:

1. Atomically transition from `accepting` to `closing`.
2. Queue the session-end event into its reserved slot without waiting.
3. Add a flush marker to the second reserved slot.
4. Have the writer acknowledge the marker only after both destination attempts
   for every earlier record finish.
5. Wait only for the remaining shutdown budget.
6. Transition to `closed` and exit even if storage remains blocked.

If either reserved-slot invariant is violated, drop the affected control record
and continue shutdown within the same deadline. Best-effort telemetry cannot
promise both guaranteed session-end delivery and a bounded exit.

A starting budget of 250 milliseconds is appropriate for low-volume local
storage. Make it a named constant and test the upper bound. Abrupt termination
may lose queued events; this is an accepted consequence of best-effort
telemetry.

### 6.4 Filesystem capability gate

Before enabling shared telemetry, validate the actual deployed filesystem—not
only a local ext4 test environment—for:

- `O_APPEND`, `O_NOFOLLOW`, and `O_NONBLOCK` behavior;
- nonblocking advisory-lock behavior across processes;
- sticky and setgid directory semantics for the chosen access profile;
- ownership, link-count, and permission reporting through `fstat`;
- rename behavior used by retention/rotation.

If any required guarantee is unavailable or inconsistent on the edge-node
mount, keep private telemetry and disable the shared writer. Document the
validated filesystem type and mount assumptions in the application-specific
adaptation.

## 7. Storage layout and permissions

### 7.1 Private copy

```text
{APP_HOME}/                       mode 0700
└── telemetry/                    mode 0700
    └── events.jsonl              mode 0600
```

The private copy is the user's fallback when shared storage is missing or
unwritable. Do not weaken existing private application-directory permissions.

### 7.2 Shared copy

Choose one access profile explicitly; do not describe the shared tree as
"operator-only" unless its permissions enforce that claim.

#### Portable local-readable profile

```text
{SHARED_TELEMETRY_DIR}/           trusted deployment owner, mode 0755
└── users/                        trusted deployment owner, mode 1777
    ├── alice.jsonl               owned by alice, mode 0644
    └── bob.jsonl                 owned by bob, mode 0644
```

This profile is simple and portable, but **every local user can enumerate
usernames and read every telemetry event**. Use it only when the approved event
catalog is explicitly classified as safe for all local edge-node users.

#### Restricted operator-group profile

On filesystems that preserve Linux setgid/sticky semantics:

```text
{SHARED_TELEMETRY_DIR}/           deploy owner, operator group, mode 0751
└── users/                        deploy owner, operator group, mode 3753
    ├── alice.jsonl               owned by alice, operator group, mode 0640
    └── bob.jsonl                 owned by bob, operator group, mode 0640
```

Other users have write/search but not list permission on `users/`; the setgid
bit makes new files inherit the operator group, and the sticky bit constrains
deletion. Validate this behavior on the deployed filesystem. A default ACL may
be used instead when it provides the same create, read, list, and delete
contracts. If neither profile is acceptable, disable shared telemetry.

The sticky bit prevents users from deleting each other's files, but it does not
prevent a user from pre-creating another username's file or symlink. The writer
must therefore treat every entry under `users/` as untrusted.

The deployment/update script, not the application user, creates the shared
parent directories. Normalize new files to the selected profile's final mode
after validation so a restrictive user `umask` does not make telemetry
unreadable. Username pre-creation remains a denial-of-service vector: an
attacker can reserve another user's filename, but safe ownership checks ensure
the victim skips it instead of writing. Detect and alert on owner/filename
mismatches operationally.

## 8. Safe append algorithm

For the private file:

1. Create/enforce owner-only parent directories.
2. Open descriptor-based with
   `O_APPEND | O_CREAT | O_WRONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW`,
   passing initial creation mode `0600`.
3. Through `fstat`, require a regular, singly linked file owned by the current
   effective user.
4. Enforce mode `0600` with `fchmod`.
5. Attempt a nonblocking exclusive `flock`.
6. Skip the append if validation fails or the lock is contended.
7. Use the same bounded UTF-8 write loop and close discipline as the shared
   path.

For the shared file:

1. Resolve identity from the effective UID through the OS account service, then
   derive one deterministic safe filename token.
2. Open with `O_APPEND | O_CREAT | O_WRONLY | O_CLOEXEC | O_NONBLOCK`, passing
   initial creation mode `0600`.
3. Add `O_NOFOLLOW`; if unavailable on the target platform, do not enable the
   shared writer without an equivalent safe-open mechanism.
4. Before writing, use `fstat` and require:
   - a regular file;
   - ownership by the current effective user;
   - link count exactly `1`;
   - the expected operator group when using the restricted profile.
5. Apply the selected final mode (`0644` or `0640`) with `fchmod`.
6. Attempt `LOCK_EX | LOCK_NB`.
7. Write one UTF-8, size-limited JSON record followed by exactly one LF.
   Retry `EINTR` and loop until all bytes are written or a hard error occurs.
8. Close the descriptor on every path.

`O_NONBLOCK` is required before `fstat`: opening an attacker-planted FIFO with
write-only blocking semantics can otherwise stall the sole writer indefinitely.
Skip symlinks, FIFOs, sockets, devices, directories, files owned by another
user, multiply linked files, and contended files. `O_NOFOLLOW` does not stop
hardlinks; requiring `st_nlink == 1` is therefore mandatory. Also verify that
the host enables protected-hardlink behavior, or disable the shared writer.

Cap each serialized event, for example at 8 KiB, so one event cannot create an
unbounded write or memory cost.

## 9. Event contract

### 9.1 Envelope

Each line contains one UTF-8 JSON object followed by one LF. Apply the size cap
to encoded bytes, not characters.

| Field | Type | Requirement |
|---|---|---|
| `schema_version` | integer | Start at `1`; increment only for incompatible envelope changes |
| `ts` | string | UTC ISO-8601 timestamp ending in `Z` |
| `event` | string | Closed catalogued event name |
| `user` | string | Validated OS username |
| `session_id` | string | Random UUID scoped to one process/session |
| `app_version` | string | `{APP_VERSION}` |
| `props` | object | Event-specific allowlisted properties only |

Example:

```json
{"schema_version":1,"ts":"2026-07-12T17:00:00Z","event":"action_completed","user":"alice","session_id":"52af9c9e-15ad-4a24-98ff-4d0635617b43","app_version":"2.3.1","props":{"action":"export","target_type":"csv"}}
```

### 9.2 Event catalog template

Start with the smallest catalog that answers an operator question:

| Event | Emit when | Allowed properties |
|---|---|---|
| `session_start` | Application becomes usable | `launch_context` using a non-sensitive basename or enum |
| `session_end` | Normal shutdown begins | `duration_s` |
| `surface_viewed` | A meaningful top-level surface opens | `surface` from a closed enum |
| `action_attempted` | Durable action record exists and execution is about to start | `action`, non-sensitive type/category fields |
| `action_refused` | Validation or policy prevents an action | `action`, `reason` from a closed enum |
| `action_cancelled` | User requests cancellation | `action`; add an opaque record ID only when an approved question requires correlation |
| `action_completed` | Application observes a terminal success | `action`, non-sensitive result category |
| `action_failed` | Application observes a terminal failure | `action`, stable error category; never raw error text |

Rename events to match the application's domain language. Do not expose a
generic `emit(name, dict)` API to product code. Provide event-specific
functions with typed or runtime-validated arguments so unknown keys cannot
reach storage.

### 9.3 Identity

- Resolve the effective UID through the OS account service/NSS; do not trust
  `USER`, `LOGNAME`, or another mutable environment variable as identity.
- Reject empty, overlong, or control-character-bearing account names rather
  than persisting or rendering them.
- Do not use an email address, display name, Kerberos principal, or durable
  machine fingerprint unless separately reviewed and approved.
- A session ID correlates events within one run without creating a durable
  device identifier.
- Capture the resolved username once per event.
- Derive the shared filename from a documented collision-free encoding of that
  username, such as base64url without padding. Require the resulting token to
  match a conservative ASCII grammar such as `[A-Za-z0-9_-]{1,172}`.
- Readers must verify that the filename token equals the encoding of each
  record's `user`; a mismatch is malformed/forged input.

## 10. Privacy and data minimization

Before accepting an event, answer:

1. What operator or product question does this event answer?
2. Is every property required to answer it?
3. Can each free-form string become a closed enum or stable category?
4. Could the value reveal content, credentials, identity beyond OS username,
   or filesystem structure?

Never log:

- query, document, script, or clipboard contents;
- absolute paths to user-owned inputs or outputs;
- email addresses, message bodies, tokens, passwords, tickets, or secrets;
- arbitrary exception strings, command output, or environment dumps;
- complete form dictionaries or serialized domain objects.

Basenames can still contain sensitive information. Prefer a category or boolean
over a basename whenever the product question does not require the name.

## 11. Configuration and opt-out

Use application-specific variables:

| Variable | Effect |
|---|---|
| `{ENV_PREFIX}_TELEMETRY=0` | Disable private and shared writes |
| `{ENV_PREFIX}_TELEMETRY_DIR` | Override `{SHARED_TELEMETRY_DIR}` exactly |

Treat case-insensitive `0`, `false`, `off`, and `no` as disabled. The default
may be enabled only after the application owner approves the event catalog and
documents the behavior for users.

The override and CLI `--dir` both identify the telemetry directory whose direct
child is `users/`; neither accepts the application root or the `users/`
directory itself. An override is trusted operator configuration, but safe
filename and file-type checks still apply.

Opting out stops future writes; it does not delete existing private or shared
records. Document separate deletion procedures and authorization.

## 12. Aggregation CLI

Recommended commands:

```text
{APP_COMMAND} telemetry who [--days N] [--dir PATH]
{APP_COMMAND} telemetry summary [--days N] [--dir PATH] [--user NAME]
```

`who` reports distinct users, session counts, last-seen timestamps, and counts
of the application's primary action.

`summary` reports feature/surface usage, action categories, refusals, failures,
and cancellations without exposing raw event payloads.

Reader requirements:

- Prefer the shared source when it contains event files; otherwise use the
  current user's private file.
- Never read both dual-written copies in one report or counts will double.
- Inspect only expected `*.jsonl` entries. For each entry, open a descriptor
  with `O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW`, then require through
  `fstat` a regular, singly linked file. Do not use a separate `lstat` safety
  check followed by a path-based open.
- Resolve/encode `--user` with the same identity-token function used by the
  writer before constructing a path.
- Stream files line by line rather than loading an unbounded history.
- Bound line length and skip oversized or malformed records.
- Validate the complete envelope, event name, and event-specific property
  schema. Reject unknown keys, terminal/control characters, and unbounded
  dimension values.
- Require every shared record's encoded `user` to match its filename token.
- Resolve that record identity through NSS and require the opened file's
  `st_uid` to equal the resolved UID. Reject the whole file on owner/identity
  mismatch before aggregating any line.
- Ignore unsupported schema versions with a visible warning.
- Parse timestamps strictly. Include records where `ts >= now - days`; reject
  implausible future timestamps outside a documented clock-skew allowance.
- Count sessions by distinct `(user, session_id)` values observed on valid
  `session_start` events. Compute `last_seen` from the maximum accepted event
  timestamp. Count each valid action event once.
- Sort output deterministically.
- Keep malformed lines isolated; one bad line must not discard the file.
- Escape or remove terminal control characters before rendering any value.

The telemetry CLI must not initialize or launch the interactive application.
Treat shared telemetry as untrusted self-reported product data, not an audit
log: each user owns and can edit their own JSONL file.

## 13. Instrumentation seams

Instrument domain boundaries, not low-level UI mechanics:

| Seam | Guidance |
|---|---|
| Session | Emit start only when the app is usable; emit end during normal shutdown |
| Navigation | Emit when a meaningful top-level surface is actually shown |
| Action attempt | Emit after a durable action record is created and immediately before execution is attempted |
| Refusal | Emit on hard validation, capacity, authentication, or policy refusal |
| Cancellation | Emit after user confirmation and when cancellation is requested |
| Completion/failure | Emit once when the terminal state becomes known |

Centralize navigation and action helpers so alternate entry points cannot bypass
instrumentation. For example, a row activation, keyboard shortcut, sidebar
item, and command-palette action should all call the same instrumented
navigation method.

Telemetry must not alter whether an action is accepted, when it becomes
durable, how it executes, or how it is recovered.

## 14. Failure policy

All telemetry failures are local and non-fatal:

| Failure | Behavior |
|---|---|
| Queue full | Drop new event; debug log |
| Invalid event/catalog value | Drop event; debug log |
| Invalid username/path component | Drop event; debug log |
| Missing/unwritable shared telemetry directory | Keep private attempt; skip shared |
| Unsafe shared target | Skip shared; do not modify target |
| Lock contention | Skip that destination |
| Writer exits unexpectedly | Disable telemetry for the process; debug log; do not spin-restart |
| Malformed historical line | Skip line during aggregation |
| Shutdown deadline exceeded | Exit and accept queued-event loss |

Do not show telemetry failures as user-facing application errors unless an
operator explicitly runs a telemetry diagnostic command.

## 15. Retention and operations

JSONL files are append-only from the application's perspective. Before rollout,
choose and document:

- expected events per session and sessions per day;
- maximum acceptable bytes per user;
- separate private and shared retention periods;
- who may read the shared directory under the selected access profile;
- how shared files are archived or removed by the deployment/operator account;
- how each user cleans private telemetry through an application command or
  uninstall workflow;
- whether uninstall preserves or removes private history;
- how an opted-out user requests deletion of already-recorded shared data.

Use an operator-owned rotation or cleanup process for the shared tree. Provide
an idempotent user-context command for private cleanup because operators
normally cannot traverse mode-`0700` homes. Because the writer opens the file
for each append rather than retaining a permanent descriptor, rename rotation
is safe between events only after validating rename/lock behavior on the
deployed filesystem. Never let application startup synchronously scan or rotate
a large telemetry tree.

Operational checks should report:

- shared directory owner and modes;
- count and total size of user files;
- unsafe entries such as symlinks or non-regular files;
- owner, group, link-count, and filename/payload identity mismatches;
- oldest/newest event timestamps;
- malformed and unsupported-schema line counts.

## 16. Required test matrix

### 16.1 Event and privacy tests

- Every public event helper writes exactly its documented keys.
- Invalid event values are dropped without raising.
- Opt-out creates no files and starts no writer.
- Session IDs are stable within a run and reset between runs.
- Identity comes from effective UID/NSS even when user environment variables
  are spoofed.
- Root-directory or unusual launch contexts do not leak absolute paths.
- Serialized records respect the event-size cap.

### 16.2 Queue and lifecycle tests

- A deliberately slow append does not delay the producer call.
- Queue saturation drops events without blocking.
- A full queue makes `flush(timeout)` wait no longer than the given deadline.
- A data-full queue retains both reserved shutdown slots.
- Normal shutdown flushes events queued before the marker.
- Shutdown atomically rejects post-marker producer events.
- A blocked writer cannot prevent process exit.
- A consumer crash disables telemetry for the process without a restart loop
  and is reported diagnostically.
- Test fixtures flush/reset writer state and isolate shared storage.

### 16.3 Filesystem adversarial tests

- A planted private or shared symlink does not modify its target.
- A planted hardlink or multiply linked target is skipped.
- A private or shared FIFO, socket, directory, or device is skipped without
  blocking.
- A file owned by another effective user is skipped.
- A restrictive `umask` still produces the selected shared mode (`0644` or
  `0640`).
- A contended private or shared lock is skipped promptly.
- Username traversal and separators cannot escape `users/`.
- Shared failure does not prevent the private append.
- Private failure does not prevent the shared append.
- Inject `ENOSPC`, `EACCES`, `EMFILE`, `EINTR`, and short writes at open/write
  boundaries and verify bounded, independent failure behavior.

Ownership, device-node, setgid, and protected-hardlink tests may require a
container or privileged fixture. Mark those as environment-specific integration
tests; do not replace them with mocks alone before edge-node rollout.

### 16.4 Aggregation tests

- Dual-written events are counted once.
- Time and user filters are correct at boundary timestamps.
- Malformed, oversized, and unsupported-schema lines are isolated.
- Symlinks and non-regular files are not read.
- Multiply linked files are not read.
- Filename/payload identity mismatches and terminal-control values are rejected.
- Filename/payload identity/file-owner UID mismatches reject the entire file.
- Empty reports are clear and deterministic.
- The telemetry subcommand never launches the interactive app.

### 16.5 End-to-end test

Under a temporary private root and shared telemetry directory:

1. Start the application.
2. Navigate to one meaningful surface.
3. Attempt one representative action.
4. Open its detail/status surface.
5. Quit normally.
6. Assert the expected start, view, action, and end events.
7. Assert private and shared records match.
8. Assert private/shared modes are `0600` and the selected shared mode.
9. Run `who` and `summary` and verify operator-readable output.
10. Repeat with telemetry disabled and assert no files are created.

## 17. Rollout checklist

- [ ] Replace every adaptation placeholder.
- [ ] Choose and disclose the portable local-readable or restricted
      operator-group access profile.
- [ ] Document the approved event catalog and the question each event answers.
- [ ] Complete privacy/security review.
- [ ] Add typed event-specific helpers.
- [ ] Keep producer paths free of filesystem I/O.
- [ ] Implement the bounded queue, serialized background consumer, reserved
      shutdown capacity, and bounded flush.
- [ ] Implement safe private and shared append contracts.
- [ ] Add aggregation commands without changing the default app launch.
- [ ] Create the shared directory from the trusted deployment/update path.
- [ ] Validate locking, safe-open, sticky/setgid, ownership, and rotation
      semantics on the real shared filesystem.
- [ ] Add all applicable adversarial tests.
- [ ] Run the end-to-end test with local infrastructure substitutes.
- [ ] Verify behavior over the real edge-node filesystem and user `umask`.
- [ ] Document retention, ownership, opt-out, and operator access.
- [ ] Pilot with a small user group before fleet rollout.

## 18. Acceptance criteria

The adaptation is ready when:

- application actions remain responsive under slow, contended, missing, and
  hostile telemetry storage;
- telemetry cannot append through a shared symlink, hardlink, or non-regular
  file;
- shared records are readable to exactly the reader population disclosed by
  the selected access profile;
- ordinary private and shared failures are independent, subject to the
  documented uninterruptible-filesystem exception;
- the event API cannot persist arbitrary properties;
- reports count a dual-written event once;
- opt-out prevents every future private and shared write without implying
  deletion of existing records;
- shutdown has a measured upper bound;
- end-to-end evidence shows the intended events and no prohibited data;
- no application lifecycle or durability behavior changes.

## 19. Worked example

Dispatch's application-specific design is documented in
[`docs/superpowers/specs/2026-07-11-usage-telemetry-design.md`](superpowers/specs/2026-07-11-usage-telemetry-design.md).
Use it as an example of adapting this guide's generic sessions, surfaces,
actions, refusals, and cancellation seams to one domain.
