# Platform support and verification

This branch supplies core adapters for macOS, Linux and Windows. A passed fixture is evidence for that tested path; native support is published only after its required gates pass on the named OS. Supplier smoke tests are separate and have **not** been run.

## Recorded evidence

| Capability | macOS 27.0.1 | Ubuntu 26.04.1 / GNOME | Fedora 43 Server | Windows 11 build 26200 |
| --- | --- | --- | --- | --- |
| Core selection, installed wheel, metadata helpers | PASS | PASS | PASS | PASS |
| Process timeout, owned children, pipe limits | Native POSIX PASS | Native POSIX PASS | Native POSIX PASS | Native Job Objects PASS |
| Lock exclusion and crash recovery | Native shlock and FD PASS | Native FD PASS | Native FD PASS | Native file-range PASS |
| Private files and task history | Native owner/mode PASS | Native publication PASS | Native publication PASS | Native DACL/WAL/SHM PASS |
| Password store | Synthetic Keychain PASS | Synthetic Secret Service PASS; KWallet UNVERIFIED | Explicit file backend PASS; desktop stores unavailable | Synthetic Credential Manager PASS in interactive user session |
| Background lifecycle | Temporary LaunchAgent PASS | Temporary systemd user unit PASS | Temporary systemd user unit PASS | Temporary same-user Task Scheduler entry PASS |

The frozen runtime/test tree in commit `668fb320f8a1e78571875283b15eb5b5f05454e7` passed **49/49** scripts on Mac Python 3.14.7 and **26/26** on each actual Windows 11 Python 3.9.25/3.13.15 environment, with all required native gates and no UNVERIFIED result. Windows native runs used the current user's least-privilege InteractiveToken; a plain SSH network logon cannot access that user's Credential Manager and is not counted as verification.

Ubuntu and Fedora full runs used Python 3.9.25/3.13.16: **39/39** applicable scripts per Ubuntu version and **38/38** per Fedora version, plus Fedora's explicit private-file credential roundtrip. These full Linux reports belong to the earlier frozen tree `e0d92902d912e8590b8461d301140c9cf486953e`. Linux production code and native gate scripts are byte-identical in `668fb32`; its changed portable credential/packaged fixtures and owned-process regression also passed on both versions on both Linux hosts (12 script executions). The earlier full reports are not relabeled as new-tree full runs. All native stores and services used isolated synthetic state. See the [release audit](superpowers/audits/2026-10-04-native-platform-release-audit.md) for the evidence scope.

GitHub Actions covers Ubuntu 22.04/24.04, macOS 14 and Windows Server 2022 with Python 3.9/3.13. Those hosted environments are separate from the manual native environments above. Initial failing runs are diagnostic evidence; release requires every applicable check on the latest PR HEAD to pass. Hosted Windows Server results never count as Windows 11 results. Supplier-authenticated opening and live quota smoke remain **NOT RUN**.

## Installation and choosing components

Python 3.9 or later is required. New profiles select **only Codex**, official Codex opening, native metadata queries, automatic opening on, Feishu off, and one-element chains with no fallback. Existing unsaved Mac installations retain their established runtime until migration is saved and explicitly applied. The personal migration is not a new-user default.

```sh
uv sync --locked                         # core only
uv sync --locked --extra feishu          # only when the bot listener is selected
uv sync --locked --extra secret-service  # Linux: only for selected Secret Service references
uv sync --locked --extra kwallet         # Linux: only for selected KWallet references
quota-sentinel --state-dir /new/state configure --new-installation
quota-sentinel config show
quota-sentinel config validate
```

For an installed wheel, choose the same optional extras with your Python package installer. Installation never obtains a supplier login. Clients remain separately installed software; checks cover only selected clients. Opening and query fallback orders are independent. Removing a provider from the active selection retains its state, history and credentials. Pi opening for Antigravity requires `pi install npm:pi-antigravity`; ClinePass requires `pi install npm:pi-clinepass-provider`, including when Pi is a fallback. Pi live metadata queries additionally require Node and a compatible explicitly selected Pi SDK/plugin; they never send a model prompt or create a model session.

## Credentials and sessions

The main configuration contains references, never credential values. Official Codex/agy continue to own their own login. No native Codex auth file is required by the account/rate-limit RPC path.

- macOS: existing generic-password service/account names remain valid. Reads and writes are bounded; locked/unavailable entries fail explicitly.
- Windows: `system` references use the current user's Generic Credential entries. Background tasks use that same user's **InteractiveToken**, without storing a task password. Logged-out operation is not promised.
- Linux: explicitly choose `system` locators `secret-service:<logical-service>` or `kwallet:<logical-service>`. Their libraries are selected extras. An unlocked password service and corresponding D-Bus session are required; the software does not unlock a store or fall back to a plaintext backend.
- Headless hosts: explicitly choose `environment` or `file` references. Environment values must be provided in the service's own session; service templates never copy secret environment values. Dedicated files require owner-only access (native Windows DACLs, POSIX mode 0600). An insecure source is refused.

For example, a reference can be `{"kind":"environment","locator":"FEISHU_APP_SECRET","account":"quota-sentinel"}` or `{"kind":"system","locator":"secret-service:quota-sentinel.feishu-app-secret","account":"quota-sentinel"}`. The public `CredentialStore.write(reference, value)` API accepts values over an isolated worker's stdin; use a password prompt rather than putting values in command arguments. `CredentialStore.read` and `delete` use the same selected reference. Setting up references and storing values is explicit.

## Background services

Save and inspect a profile first. Install prepares the current-user entry; start applies it. Configuration saving alone does not stop or restart a running service.

```sh
quota-sentinel service install
quota-sentinel service start
quota-sentinel service stop
quota-sentinel service uninstall
quota-sentinel serve                     # foreground, including non-systemd Linux
```

Only opening starts one scheduler; only the bot starts one listener; selecting both shares one scheduler. Manual-only profiles install no service. Stop/uninstall also work after background features are disabled. Uninstall retains all supplier state, task history and credentials.

macOS uses a LaunchAgent. Explicit start of `quota-sentinel.service` retires only this app's three known old entries, then applies the selected host. The compatibility shell installer delegates saved profiles to this host and installs only their selected extras; an unsaved deployment retains its historical listener setup. The current personal deployment has **not** been applied or restarted during development.

Linux uses a systemd user unit with control-group cleanup and a derived stop deadline. Without an available user manager, installation gives foreground guidance. A desktop password service's D-Bus session is not automatically guaranteed by a systemd user service. Windows uses a same-user logon task and a restricted nonsecret launch manifest; task stop can be abrupt, and each owned client tree is contained by a Job Object.

Service output is written to private `logs/service.out.log` and `logs/service.err.log` on macOS/Windows; systemd keeps its journal. Startup does not resolve packages over the network. [launchd lifecycle options](https://raw.githubusercontent.com/apple-oss-distributions/launchd/main/man/launchd.plist.5), [systemd service execution](https://raw.githubusercontent.com/systemd/systemd/v249/man/systemd.service.xml), and [Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema) document the underlying controls.

## Running verification

Bootstrap pinned build tools once from public PyPI, then test installed runtime execution **--offline**. The regression dispatcher keeps each HOME private and passes only an explicit public build-cache coordinate to wheel verification.

```sh
uv --no-config build --wheel --out-dir .ci-build
uv run --frozen --no-sync python tests/run-regressions.py --platform portable
uv run --frozen --no-sync python tests/run-regressions.py --platform current --require-native --report native-evidence.json
```

Reports separate `platform-independent`, `native-platform` and `supplier-smoke` evidence. Exit 77 in a native gate is **UNVERIFIED**, never PASS; a required incomplete native gate returns a nonzero dispatcher result. An omitted native OS/capability has no support claim. Linux native service verification is opt-in (`QUOTA_SENTINEL_TEST_LINUX_SERVICES=1`) and uses a temporary unique unit name in the current user manager. Linux password-store integration requires an explicitly synthetic `dbus-run-session`/keyring environment; unavailable desktops/services remain unverified.

The workflow covers Ubuntu 22.04/24.04, hosted Mac and Windows Server on Python 3.9/3.13, core/selected extras, sanitized logs, and no supplier credentials. Windows 11 verification requires the explicitly configured self-hosted `quota-sentinel-win11` runner or a recorded native manual run with `--require-windows11`; Server is refused as proof. Native CI contexts and this current local Mac are recorded separately.

POSIX cleanup owns the launched process group and observed descendants; extremely rapid intentional daemonization outside those groups is not a general sandbox guarantee. Windows assignment happens before the child is resumed. File bytes are flushed before replacement; Windows directory durability depends on the OS/filesystem and is not claimed as POSIX directory fsync. Shared state cannot mix lock protocols; move it only while every service is stopped and explicitly reprovision the target native protocol.
