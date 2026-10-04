# Native platform release audit — 2026-10-04

## Source and scope

The final runtime and tests are committed as `668fb320f8a1e78571875283b15eb5b5f05454e7`, Git tree `172974ca794d6977a0e551813c60c00a35297d2c`. The following documentation update changes prose only; it preserves every tested runtime, test, workflow, lockfile and resource blob. Native reports and raw logs are retained privately because they contain machine paths; they are not committed to this repository.

All fixtures used synthetic credentials, fake supplier clients and uniquely owned temporary state/tasks. No real credential value was read, supplier model called or Feishu message sent. Existing personal configuration and deployed services were preserved. No user device was registered as a GitHub runner. Review and native diagnosis in this final stage were performed by the primary agent; stopped subagents were not resumed.

## Closed release findings

| Area | Repair and verification |
| --- | --- |
| Windows process cleanup | Job accounting could reach zero before a descendant released its file. Capture synchronization handles, verify actual Job membership, terminate the owned Job and wait for descendants under one shared deadline. A real sleeping descendant/file fixture failed before the repair and passed afterward. |
| Windows cleanup deadline | PID snapshot work could bypass that deadline. Deadline checks now cover enumeration and per-process inspection, with Job termination even if the snapshot times out. A controlled six-descendant native test exceeded the bound before repair; afterward it raises the expected timeout and verifies every descendant exits. |
| Python 3.9 fixture startup | Windows children with an otherwise empty environment require SYSTEMROOT for interpreter entropy initialization. Isolated test environments preserve that OS variable without importing ambient tokens; quota/credential assertions remain active. Production environment selection is unchanged. |
| Native task fixture cleanup | Repeat actual directory removal within a bounded deadline, ignore only already-disappeared children, and fail permanent sharing violations. Verify that the directory really disappears instead of relying on a detached TemporaryDirectory finalizer. |
| Private publication | Preserve owned file/stream semantics, reject POSIX FIFOs promptly, bound Windows sharing retries, and publish UTF-8/LF quota state through the private file primitive. |
| Service configuration | Keep applied snapshots private, render native UTF-16 task XML, use valid literal systemd working paths, preserve safe existing shared service directory permissions and retain selected-platform readiness checks. |

Focused controlled cases demonstrated the defects before their fixes. Full frozen verification below followed the final source change. No known P1–P4 finding remains open in the reviewed scope; this is not a guarantee about untested supplier implementations.

## Native verification

| Environment | Python | Result |
| --- | --- | --- |
| macOS 27.0.1 arm64 | 3.14.7 | 49/49 scripts; required native gates PASS; UNVERIFIED 0 |
| Windows 11 build 26200 | 3.9.25, 3.13.15 | 26/26 scripts each; required native gates PASS; UNVERIFIED 0 |
| Ubuntu 26.04.1 with GNOME | 3.9.25, 3.13.16 | Earlier Linux freeze: 39/39 applicable scripts each; synthetic Secret Service and systemd lifecycle PASS |
| Fedora 43 Server, SELinux enforcing | 3.9.25, 3.13.16 | Earlier Linux freeze: 38/38 applicable scripts each; systemd lifecycle and explicit private-file credentials PASS |

Mac and Windows full results bind to tree `172974ca794d6977a0e551813c60c00a35297d2c`. Full Linux results bind to `e0d92902d912e8590b8461d301140c9cf486953e`; all Linux production and native gate blobs are unchanged in the final runtime commit. Final-tree portable credential/packaged fixtures and owned-process checks passed on both versions on both Linux hosts, 12/12 script executions. This distinction preserves each report's actual source binding.

Windows native execution used a same-user least-privilege interactive task. Credential Manager is unavailable under a plain SSH network logon; that limitation was diagnosed and not converted into a passing skipped gate. Logged-out background operation remains unsupported. Test tasks and processes were removed after execution. Fedora had no desktop Secret Service/KWallet; its explicitly selected file backend was verified instead, without automatic plaintext fallback. KWallet remains unverified. Supplier-authenticated smoke remains **NOT RUN**.

## Publication checks and integration gate

- Staged changes: no secret scanner finding. Complete all-ref history: only a verified synthetic test credential match, no unexpected finding. The previously removed private blob remains unreachable.
- Ignore rules: 59/59 representative checks pass, with no tracked ignored file. Compile and whitespace checks pass.
- Original checkout, saved configuration checksum and deployed service process remain unchanged.
- GitHub Actions must pass every applicable job on the latest PR HEAD before integration. Its Windows Server jobs are portability checks, not Windows 11 proof. The optional Windows 11 runner job is not required when it was not dispatched; the actual manual native proof above is separate.
- Integration uses a merge commit to retain development history, without squash or force push. This audit records readiness for that gate; it does not claim an unobserved CI or merge result.
