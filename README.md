# Hermes Watcher for Omarchy

A local-only Omarchy Shell service and bar widget that tracks turns from local Hermes Agent profiles and sends a desktop notification when eligible turns finish.

## Components

- `hermes-plugin/`: opt-in Hermes observer using documented turn and API lifecycle hooks.
- `hermes_bot_status.py`: validates and aggregates records, detects open Hermes processes and stale writers, reads bounded session metadata for recent-session launchers, persists notification delivery claims and acknowledgements, and prunes history.
- `Service.qml`: persistent Omarchy Shell service that polls the collector and asks the collector to deliver notifications with `notify-send` argv and the bundled Hermes Watcher icon (never a shell string).
- `BarWidget.qml`: online-session badge and panel with one Agent card per open Hermes session, each profile's native Bot Mode avatar, current work descriptions, live context pressure, one-click launchers for every local profile, and up to six resumable recent sessions. Its menu-bar branding uses the official Hermes Agent favicon SVG, tinted to the active Omarchy foreground color.
- `scripts/setup-profiles`: installs a stable observer copy under XDG data, then links and enables it for the default and every named local profile.
- `scripts/remove-profiles`: disables the observer, removes only links created by setup, and removes the stable observer copy.
- `scripts/cleanup-observer`: removes the observer after a clean Omarchy disable/removal while avoiding cleanup during a shell restart.

## Privacy and safety

The observer stores profile name, opaque session/turn/task IDs, state, timestamps, model, the sanitized runtime reasoning-level label, platform, writer process identity, numeric context usage, and a whitespace-normalized excerpt of up to 160 characters from the current user request. That excerpt powers the Agent card's work description; the rest of the prompt is not stored. The collector reads only the minimum local `/proc` command prefix needed to recognize an interactive Hermes process and its explicit profile flag; it stops before one-shot prompts and resume names. For recent-session launchers, it opens each owner-controlled Hermes `state.db` read-only and selects only session ID, generated title, source, and activity timestamps; it never reads the messages table or transcript content. Snapshots expose one row per open session, profile names, uptime, context pressure, the bounded work description, profile avatars, and up to six recent local session titles plus the exact IDs required by Hermes resume. Launchable profiles must match Hermes' canonical lowercase profile-ID rules; reserved and tombstoned profiles are excluded. Avatar lookup accepts only an owner-controlled regular PNG, JPEG, or WebP file in the profile's `assets` directory, refuses symlinks, and opens special files non-blockingly. It does not store responses, tool arguments/results, conversation history, working directories, or credentials.

Records live under:

```text
${XDG_STATE_HOME:-~/.local/state}/omarchy/hermes-bots/
```

Directories are mode `0700`; JSON records and consumer state are mode `0600`. Writes use atomic replacement and synchronize both the file and containing directory. Observer errors are logged and fail open so monitoring cannot block a Hermes turn.

The plugin reads only bounded session metadata from `state.db` for the Recent launcher and does not read the messages table. To guarantee polling never creates or updates SQLite coordination files, every session query uses an immutable read of the latest checkpoint; newly written titles or activity can therefore appear after Hermes' next checkpoint rather than directly from an uncheckpointed WAL frame. The collector mirrors Hermes' user-visible session boundary by excluding delegated and internal children while retaining roots, branch/reset conversations, and resumable compression tips; a compression predecessor remains visible until an eligible tip exists. Invalid IDs, titles, and timestamps are rejected before result limits or continuation ranking. It does not register model-facing tools, inject prompts, alter approvals, edit Hermes `config.yaml`, or write to `/usr/share/omarchy`. Context-window capacity is resolved through Hermes' own model metadata resolver; no prompt, response, or credential is supplied to that lookup.

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

Terminal lifecycle history is automatically bounded to the 100 newest records during hourly pruning. Removing the plugin does not delete the separate lifecycle state directory.

`HERMES_ROOT` may be set for tests or nonstandard base-home layouts. It defaults to `~/.hermes`. The collector publishes its absolute launch root to the local service, which passes it to new terminals as an argv-safe `HERMES_HOME` environment assignment; this keeps a selected row and the resumed database aligned without shell interpolation. `XDG_STATE_HOME` and `XDG_DATA_HOME` are honored.

## Defaults

- Poll every 2 seconds.
- Notify on success and failure.
- Do not notify on interruption or stale writer by default.
- Suppress notifications for turns shorter than 5 seconds.
- Catch up unacknowledged completions for at most 1 hour.
- Show at most 6 recent local sessions.
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
python3 hermes_bot_status.py initialize
python3 hermes_bot_status.py acknowledge EVENT_ID
python3 hermes_bot_status.py prune --keep-terminal 100
```

`initialize` acknowledges existing terminal history and is run by `setup-profiles`, preventing a first-install notification storm.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
node tests/model.test.js
hermes plugins doctor --ci hermes-plugin
omarchy plugin validate .
```

The tests cover sanitized lifecycle writes, success/failure/interruption outcomes, hook failure isolation, stale/completion race handling, persistent delivery claims, real hung-process recovery, concurrent acknowledgements, stale-process persistence, recent-session metadata and limits, immutable SQLite sidecar races, Hermes-compatible delegate/branch/reset/compression boundaries, malformed pre-limit IDs/titles/timestamps, safe exact-session resume argv, profile filtering, history pruning, CLI JSON, Omarchy manifest/QML contracts, a real-Hermes isolated lifecycle round trip, an executable Quickshell service smoke test, and setup/removal behavior in temporary homes.

## MVP limitations

- Local Hermes profiles only; remote gateways are not monitored.
- Activity is aggregated by owning profile; delegated child trees are not displayed separately.
- Notifications contain only profile, outcome, and duration—never response previews.
- No notification click-to-open action.
- A hard-killed process becomes `stale`, not successful.
