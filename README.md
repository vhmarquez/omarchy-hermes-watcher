# Hermes Watcher for Omarchy

A local-only Omarchy Shell service and bar widget that tracks turns from local Hermes Agent profiles and sends a desktop notification when eligible turns finish.

## Components

- `hermes-plugin/`: opt-in Hermes observer using documented turn and API lifecycle hooks.
- `hermes_bot_status.py`: validates and aggregates records, detects open Hermes processes and stale writers, reads bounded session metadata for recent-session launchers, persists notification delivery claims and acknowledgements, and prunes history.
- `Service.qml`: persistent Omarchy Shell service that keeps one event-driven collector process, validates its NDJSON stream, and asks the collector to deliver notifications with `notify-send` argv and the bundled Hermes Watcher icon (never a shell string).
- `BarWidget.qml`: online-session badge and panel with one Agent card per open Hermes session, each profile's native Bot Mode avatar, current work descriptions, live context pressure, one-click launchers for every local profile, and up to six resumable recent sessions. Its menu-bar branding uses the official Hermes Agent favicon SVG, tinted to the active Omarchy foreground color.
- `scripts/setup-profiles`: installs a stable observer copy under XDG data, then links and enables it for the default and every named local profile.
- `scripts/remove-profiles`: disables the observer, removes only links created by setup, and removes the stable observer copy.
- `scripts/cleanup-observer`: removes the observer after a clean Omarchy disable/removal while avoiding cleanup during a shell restart.

## Privacy and safety

The observer stores bounded internal lifecycle identifiers, state, timestamps, model, the sanitized runtime reasoning-level label, platform, writer process identity, numeric context usage, and—when `showWorkDescription` is enabled—a whitespace-normalized excerpt of up to 160 characters from the current user request. Credential-like assignments and bearer values are replaced with `[REDACTED]` before persistence. Disabling the setting prevents new excerpts, purges existing excerpts from Watcher state, and terminal records discard excerpts automatically. An older running Hermes process is reported as needing restart when it lacks this privacy capability. The collector reads only the minimum local `/proc` command prefix needed to recognize an interactive Hermes process and its explicit profile flag; it stops before one-shot prompts and resume names. When `showRecentSessionTitles` is enabled, it opens each owner-controlled Hermes `state.db` read-only and selects only the session ID, generated title, and activity timestamps; disabling the setting prevents all session-database access. It never reads the messages table or transcript content. Public snapshots use closed, minimal DTOs and never expose raw writer identity, task/turn IDs, request identifiers, or exit reasons. Every item and array is validated, and each serialized snapshot is bounded to 256 KiB.

Records live under:

```text
${XDG_STATE_HOME:-~/.local/state}/omarchy/hermes-bots/
```

Directories are mode `0700`; JSON records and consumer state are mode `0600`. Writes use atomic replacement and synchronize both the file and containing directory. Observer errors are logged and fail open so monitoring cannot block a Hermes turn.

The plugin reads only bounded session metadata from `state.db` for the Recent launcher and does not read the messages table. To guarantee collection never creates or updates SQLite coordination files, every session query uses an immutable read of the latest checkpoint; newly written titles or activity can therefore appear after Hermes' next checkpoint rather than directly from an uncheckpointed WAL frame. The collector mirrors Hermes' user-visible session boundary by excluding delegated and internal children while retaining roots, branch/reset conversations, and resumable compression tips; a compression predecessor remains visible until an eligible tip exists. Invalid IDs, titles, and timestamps are rejected before result limits or continuation ranking. It does not register model-facing tools, inject prompts, alter approvals, edit Hermes `config.yaml`, or write to `/usr/share/omarchy`. Context-window capacity is resolved through Hermes' own model metadata resolver; no prompt, response, or credential is supplied to that lookup.

## Source installation

Install the Omarchy plugin through the supported plugin workflow or place a checkout under `~/.config/omarchy/plugins/vhm.hermes-bots/`, then enable it. The persistent service automatically runs:

```bash
scripts/setup-profiles
```

This uses `hermes plugins enable omarchy-bot-status` (profile-scoped for named profiles) and keeps the linked observer in `${XDG_DATA_HOME:-~/.local/share}/vhm.hermes-bots/`, so Omarchy source removal cannot leave dangling links. The service reconciles profiles discovered after startup and reports failed profile IDs while retrying successful profiles independently. Open Hermes sessions are visible immediately. Restart Hermes processes that were already running during first installation to add exact per-turn `Working` state and completion outcomes; Hermes loads lifecycle plugins at process startup.

On a clean Omarchy disable or removal, the service schedules observer cleanup automatically after confirming that the plugin remains disabled. Manual cleanup remains available:

```bash
scripts/remove-profiles
```

Terminal lifecycle history is automatically bounded to the 100 newest records and 30 days during hourly pruning. The panel's two-step **Clear Watcher history** action removes terminal Watcher records and their consumer entries while preserving running turns, observer handshakes, privacy policy, and all Hermes sessions in `state.db`. Removing the plugin does not delete the separate lifecycle state directory.

`HERMES_ROOT` may be set for tests or nonstandard base-home layouts. It defaults to `~/.hermes`. The collector publishes its absolute launch root to the local service, which passes it to new terminals as an argv-safe `HERMES_HOME` environment assignment; this keeps a selected row and the resumed database aligned without shell interpolation. `XDG_STATE_HOME` and `XDG_DATA_HOME` are honored.

## Defaults

- Use one persistent event-driven collector; lifecycle-file changes refresh immediately without launching another interpreter.
- Run the default 30-second health scan across `/proc`, profiles, observers, avatars, and session-database signatures.
- Use the 2-second `pollIntervalSec` only as a fallback when inotify coverage is unavailable and while checking active stale-writer deadlines.
- Enforce the idle target of no more than 0.25% of one CPU core; the executable performance test also checks one process, no descendants, bounded wakeups, and at most 64 MiB RSS.
- Notify on success and failure.
- Do not notify on interruption or stale writer by default.
- Suppress notifications for turns shorter than 5 seconds.
- Catch up unacknowledged completions for at most 1 hour.
- Show at most 6 recent local sessions.
- Show work descriptions and recent session titles by default; both can be independently disabled.
- Retain terminal Watcher history for at most 30 days and 100 records.
- Monitor every instrumented profile unless `profileFilter` is set to a comma-separated list of profile IDs.
- Mark a dead writer stale after a 30-second grace period; never infer success from a vanished process.
- Use normal notification urgency so Omarchy Do Not Disturb remains effective.
- Create a persistent delivery claim before `notify-send`, acknowledge successful delivery before returning to QML, and permit an interrupted claim to be retried after five minutes.
- Bound setup, collection, delivery, acknowledgement, launch, and pruning subprocesses; repeated setup, collection, delivery, and acknowledgement failures use bounded backoff.

Settings are declared in `manifest.json` and can be changed through Omarchy bar/plugin settings.

Left-click the bar icon to open the panel, right-click to refresh, and middle-click to toggle notifications. The Bluetooth-style panel header shows current Agent activity beneath the title and has a native notification toggle on its right edge. The `AGENTS` section begins with icon-only launchers for every local Hermes profile; hovering an icon reveals the profile name, and selecting it opens a fresh interactive session for that profile in the user's Omarchy terminal. Directly below the profile icons, the `SESSIONS` section contains native `Active` and `Recent` tabs that switch between open Agent-session cards and the six most recently active titled CLI, TUI, or Desktop sessions across local profiles. `Active` is selected by default, and left/right panel navigation switches tabs. Selecting a Recent row opens a new terminal and resumes that exact profile/session. Service IPC exposes `refresh`, `status`, `notificationsOn`, `notificationsOff`, and `notificationsToggle`.

Each Agent card represents one open Hermes session and shows its session age in unbolded parentheses beside the profile title; aggregate “open sessions” text is not repeated inside the card. The card uses equal outer padding on every side and grows with its content. Its bounded work description is collapsed to one line by default; click the card to expand the complete available description, and click again to collapse it. Working cards show the latest active turn's bounded work description and the highest context pressure among concurrent turns in that session. The compact gauge displays only the context bar; hovering over it reveals the model, sanitized runtime reasoning level, and the current or last-known used/max token counts and percentage. The reading is updated with a pre-request estimate, then replaced by provider-reported prompt usage after a successful response. When a session remains online but idle, the bar may retain only that same process instance's latest provider-confirmed reading, identified as `Last context` in its tooltip; an active turn without reliable context never falls back to stale data. The gauge is omitted when Hermes cannot resolve a positive context window, rather than fabricating a percentage.

Each Agent card uses the profile's canonical `assets/avatar.*` image. Hermes Desktop maintains this file for generated avatars, uploads, shape/blob selections, and pet selections, so changing the Bot Mode avatar is picked up automatically. A version token bypasses the image cache after changes; profiles without a safe avatar use the bundled Hermes Watcher emblem.

## Collector CLI

```bash
python3 hermes_bot_status.py snapshot
python3 hermes_bot_status.py watch
python3 hermes_bot_status.py initialize
python3 hermes_bot_status.py acknowledge EVENT_ID
python3 hermes_bot_status.py prune --keep-terminal 100 --max-age-sec 2592000
python3 hermes_bot_status.py clear-history
```

`initialize` acknowledges existing terminal history and is run by `setup-profiles`, preventing a first-install notification storm.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
node tests/model.test.js
hermes plugins doctor --ci hermes-plugin
omarchy plugin validate .
```

The tests cover sanitized lifecycle writes, privacy-policy races, closed DTO validation, total payload bounds, event-driven streaming, persistent-process refresh and shutdown, cache invalidation, the idle CPU/RSS/wakeup budget, success/failure/interruption outcomes, persistent delivery claims, stale-process persistence, recent-session metadata and privacy controls, profile filtering, count-and-time retention, safe history clearing, lifecycle round trips, an executable Quickshell service smoke test, and setup/removal in temporary homes.

## MVP limitations

- Local Hermes profiles only; remote gateways are not monitored.
- Activity is aggregated by owning profile; delegated child trees are not displayed separately.
- Notifications contain only profile, outcome, and duration—never response previews.
- No notification click-to-open action.
- A hard-killed process becomes `stale`, not successful.
