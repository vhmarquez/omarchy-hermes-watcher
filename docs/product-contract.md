# Hermes Watcher product contract

This document records the product and compatibility decisions that guide the 0.x implementation. It is not a replacement for the user-facing behavior in `README.md`.

## Domain language

- **Profile**: a configured local Hermes identity and home.
- **Session**: one running or resumable Hermes conversation.
- **Turn**: one user-to-Agent interaction inside a session.
- **Agent**: the user-facing representation of a running Hermes session. The established plugin ID `vhm.hermes-bots` remains unchanged for compatibility.

## Information boundaries

Profile filtering applies to every Watcher-visible profile surface: activity, notifications, available-profile launchers, avatars, and recent-session metadata. A separate display filter should be introduced if launchers ever need broader scope.

Task descriptions remain enabled by default for the 0.x series because live work identification is a core feature. A later privacy milestone must add a setting that prevents collection and persistence, rather than merely hiding descriptions after collection.

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

Idle performance target: no Python interpreter launch every two seconds and no more than 0.25% of one CPU core on the reference system while the panel is closed. This is Milestone 3 future work, not a Milestones 0–2 acceptance criterion; the current collector still uses bounded polling. Active updates should remain visible within two seconds unless the user configures a slower interval.
