# Hermes Watcher product contract

This document records the product and compatibility decisions that guide the 0.x implementation. It is not a replacement for the user-facing behavior in `README.md`.

## Domain language

- **Profile**: a configured local Hermes identity and home.
- **Session**: one running or resumable Hermes conversation.
- **Turn**: one user-to-Agent interaction inside a session.
- **Agent**: the user-facing representation of a running Hermes session. The established plugin ID `vhm.hermes-bots` remains unchanged for compatibility.

## Information boundaries

Profile filtering applies to every Watcher-visible profile surface: activity, notifications, available-profile launchers, avatars, and recent-session metadata. A separate display filter should be introduced if launchers ever need broader scope.

Task descriptions remain enabled by default for the 0.x series because live work identification is a core feature. Milestone 4 privacy controls now allow collection to be disabled before observer persistence, purge existing excerpts, redact credential-like values, report older observers that require restart, and independently disable recent-title database access. Public snapshot DTOs expose only documented consumer fields, enforce item schemas and a 256 KiB total budget, and retain terminal Watcher history for at most 100 records and 30 days.

## Panel semantics

Recent means resumable sessions, not completion outcomes. Completion outcomes belong in Activity, with failures receiving a visible destination before they are marked seen. That destination is Milestone 6 future work; Milestones 0–2 establish the terminology and preserve the existing failure indicator without claiming that Activity already exists.

## Compatibility target

The initial supported baseline is:

- Hermes Agent 0.21 or newer.
- Omarchy 4.0 or newer.
- Python 3.11 or newer for the observer and collector.
- Linux with procfs and the standard Omarchy runtime commands documented in `README.md`.

Compatibility-sensitive Hermes integrations must be isolated and tested against fixtures from every supported release.

## Performance target

Idle performance target: no Python interpreter launch every two seconds and no more than 0.25% of one CPU core on the reference system while the panel is closed. Milestone 3 is implemented with one persistent inotify-driven collector, cached immutable inputs, an independent 30-second health scan, and a two-second fallback only when event coverage is unavailable or an active stale-writer deadline requires it. Active lifecycle updates remain event-driven and should appear within two seconds. An executable regression test enforces the CPU target, one-process/no-descendant contract, bounded scheduler wakeups, and a 64 MiB RSS ceiling.
