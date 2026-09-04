function emptySnapshot() {
  return {
    ok: true,
    activeBotCount: 0,
    activeTurnCount: 0,
    onlineBotCount: 0,
    onlineSessionCount: 0,
    onlineProfiles: [],
    availableProfiles: [],
    hermesRoot: "",
    profiles: [],
    recent: [],
    recentSessions: [],
    pendingNotifications: [],
    lastError: ""
  }
}

function parseSnapshot(raw) {
  var text = String(raw || "").trim()
  if (text === "") {
    var empty = emptySnapshot()
    empty.ok = false
    empty.lastError = "Hermes Watcher returned empty status"
    return empty
  }
  try {
    var value = JSON.parse(text)
    if (!value || value.schemaVersion !== 1) throw new Error("unsupported snapshot")
    value.ok = true
    value.activeBotCount = Math.max(0, Number(value.activeBotCount || 0))
    value.activeTurnCount = Math.max(0, Number(value.activeTurnCount || 0))
    value.onlineBotCount = Math.max(0, Number(value.onlineBotCount || 0))
    value.onlineSessionCount = Math.max(0, Number(value.onlineSessionCount || 0))
    value.onlineProfiles = Array.isArray(value.onlineProfiles) ? value.onlineProfiles : []
    value.availableProfiles = Array.isArray(value.availableProfiles) ? value.availableProfiles : []
    value.hermesRoot = typeof value.hermesRoot === "string" && value.hermesRoot.charAt(0) === "/"
      ? value.hermesRoot : ""
    value.profiles = Array.isArray(value.profiles) ? value.profiles : []
    value.recent = Array.isArray(value.recent) ? value.recent : []
    value.recentSessions = Array.isArray(value.recentSessions) ? value.recentSessions.slice(0, 6) : []
    value.pendingNotifications = Array.isArray(value.pendingNotifications) ? value.pendingNotifications : []
    value.lastError = ""
    return value
  } catch (error) {
    var failed = emptySnapshot()
    failed.ok = false
    failed.lastError = "Failed to parse Hermes Watcher status"
    return failed
  }
}

function formatDuration(rawSeconds) {
  var seconds = Math.max(0, Math.floor(Number(rawSeconds || 0)))
  var hours = Math.floor(seconds / 3600)
  var minutes = Math.floor((seconds % 3600) / 60)
  var remainder = seconds % 60
  if (hours > 0) return hours + "h " + String(minutes).padStart(2, "0") + "m"
  if (minutes > 0) return minutes + "m " + String(remainder).padStart(2, "0") + "s"
  return remainder + "s"
}

function formatRelative(rawTimestamp, rawNow) {
  var timestamp = Number(rawTimestamp || 0)
  var now = Number(rawNow || Date.now() / 1000)
  var seconds = Math.max(0, Math.floor(now - timestamp))
  if (seconds < 60) return "just now"
  var minutes = Math.floor(seconds / 60)
  if (minutes < 60) return minutes + "m ago"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h ago"
  return Math.floor(hours / 24) + "d ago"
}

function formatTokenCount(rawTokens) {
  var tokens = Math.max(0, Math.round(Number(rawTokens || 0)))
  if (tokens >= 1000000) return (tokens / 1000000).toFixed(1).replace(/\.0$/, "") + "m"
  if (tokens >= 1000) return Math.round(tokens / 1000) + "k"
  return String(tokens)
}

function filterRecent(records, filter) {
  var recent = Array.isArray(records) ? records : []
  if (filter === "all") return recent
  var filtered = []
  for (var i = 0; i < recent.length; i++) {
    var state = String((recent[i] && recent[i].state) || "")
    if (filter === "success" && state === "succeeded") filtered.push(recent[i])
    else if (filter === "issues"
        && (state === "failed" || state === "interrupted" || state === "stale")) filtered.push(recent[i])
  }
  return filtered
}

function notificationText(record) {
  var profile = String((record && record.profile) || "Hermes Agent")
  var state = String((record && record.state) || "stopped")
  var duration = formatDuration(record ? record.durationSec : 0)
  if (state === "succeeded") return { title: profile + " finished", body: "Completed in " + duration }
  if (state === "failed") return { title: profile + " failed", body: "Failed after " + duration }
  if (state === "interrupted") return { title: profile + " stopped", body: "Stopped after " + duration }
  return { title: profile + " became stale", body: "Process exited without a completion event" }
}

function stateGlyph(state) {
  if (state === "succeeded") return "󰄬"
  if (state === "failed") return "󰅙"
  if (state === "interrupted") return "󰓛"
  return "󰋗"
}

function stateLabel(state) {
  if (state === "succeeded") return "Succeeded"
  if (state === "failed") return "Failed"
  if (state === "interrupted") return "Interrupted"
  return "Stale"
}

if (typeof module !== "undefined") {
  module.exports = {
    emptySnapshot: emptySnapshot,
    parseSnapshot: parseSnapshot,
    formatDuration: formatDuration,
    formatRelative: formatRelative,
    formatTokenCount: formatTokenCount,
    notificationText: notificationText,
    stateGlyph: stateGlyph,
    stateLabel: stateLabel,
    filterRecent: filterRecent
  }
}
