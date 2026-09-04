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

var MAX_SNAPSHOT_CHARS = 262144
var MAX_SAFE_NUMBER = 9007199254740991
var TERMINAL_STATES = ["succeeded", "failed", "interrupted", "stale"]
var REASONING_LEVELS = ["", "off", "on", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

function utf8ByteLength(value) {
  var text = String(value || "")
  var bytes = 0
  for (var i = 0; i < text.length; i++) {
    var code = text.charCodeAt(i)
    if (code <= 0x7f) bytes += 1
    else if (code <= 0x7ff) bytes += 2
    else if (code >= 0xd800 && code <= 0xdbff
        && i + 1 < text.length
        && text.charCodeAt(i + 1) >= 0xdc00
        && text.charCodeAt(i + 1) <= 0xdfff) {
      bytes += 4
      i += 1
    } else bytes += 3
  }
  return bytes
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function hasOnlyKeys(value, allowed) {
  if (!isObject(value)) return false
  var keys = Object.keys(value)
  for (var i = 0; i < keys.length; i++) if (allowed.indexOf(keys[i]) === -1) return false
  return true
}

function finiteNumber(value, minimum, maximum) {
  return typeof value === "number" && Number.isFinite(value)
    && value >= minimum && value <= maximum
}

function boundedInteger(value, minimum, maximum) {
  return finiteNumber(value, minimum, maximum) && Math.floor(value) === value
}

function boundedText(value, maximum, allowEmpty) {
  if (typeof value !== "string" || value.length > maximum || (!allowEmpty && value.length === 0)) return false
  for (var i = 0; i < value.length; i++) {
    var code = value.charCodeAt(i)
    if (code < 32 || (code >= 127 && code <= 159)) return false
  }
  return true
}

function validProfile(value) {
  return typeof value === "string"
    && (value === "default" || /^[a-z0-9][a-z0-9_-]{0,63}$/.test(value))
}

function validAvatar(value) {
  return value === undefined
    || (boundedText(value, 4096, false) && value.indexOf("file://") === 0)
}

function validContext(value) {
  var present = ["contextUsed", "contextMax", "contextPercent", "contextIsLastKnown"]
    .filter(function(key) { return value[key] !== undefined })
  if (present.length === 0) return true
  return present.length === 4
    && boundedInteger(value.contextUsed, 1, MAX_SAFE_NUMBER)
    && boundedInteger(value.contextMax, 1, MAX_SAFE_NUMBER)
    && boundedInteger(value.contextPercent, 0, 100)
    && typeof value.contextIsLastKnown === "boolean"
}

function validOnlineProfile(value) {
  var allowed = ["sessionKey", "profile", "activeTurnCount", "observerLoaded",
    "workDescriptionPolicyLoaded", "runningForSec",
    "model", "platform", "reasoningLevel", "workDescription", "avatarUrl", "contextUsed",
    "contextMax", "contextPercent", "contextIsLastKnown"]
  return hasOnlyKeys(value, allowed)
    && typeof value.sessionKey === "string" && /^[a-f0-9]{64}$/.test(value.sessionKey)
    && validProfile(value.profile)
    && boundedInteger(value.activeTurnCount, 0, 100000)
    && typeof value.observerLoaded === "boolean"
    && (value.workDescriptionPolicyLoaded === undefined
      || typeof value.workDescriptionPolicyLoaded === "boolean")
    && finiteNumber(value.runningForSec, 0, MAX_SAFE_NUMBER)
    && (value.model === undefined || boundedText(value.model, 200, true))
    && (value.platform === undefined || boundedText(value.platform, 100, true))
    && (value.reasoningLevel === undefined || REASONING_LEVELS.indexOf(value.reasoningLevel) !== -1)
    && (value.workDescription === undefined || boundedText(value.workDescription, 160, false))
    && validAvatar(value.avatarUrl)
    && validContext(value)
}

function validAvailableProfile(value) {
  return hasOnlyKeys(value, ["profile", "avatarUrl"])
    && validProfile(value.profile) && validAvatar(value.avatarUrl)
}

function validProfileSummary(value) {
  var allowed = ["profile", "activeTurnCount", "runningForSec", "model", "platform",
    "reasoningLevel", "workDescription", "contextUsed", "contextMax", "contextPercent"]
  return hasOnlyKeys(value, allowed)
    && validProfile(value.profile)
    && boundedInteger(value.activeTurnCount, 0, 100000)
    && finiteNumber(value.runningForSec, 0, MAX_SAFE_NUMBER)
    && (value.model === undefined || boundedText(value.model, 200, true))
    && (value.platform === undefined || boundedText(value.platform, 100, true))
    && (value.reasoningLevel === undefined || REASONING_LEVELS.indexOf(value.reasoningLevel) !== -1)
    && (value.workDescription === undefined || boundedText(value.workDescription, 160, false))
    && (value.contextUsed === undefined || boundedInteger(value.contextUsed, 1, MAX_SAFE_NUMBER))
    && (value.contextMax === undefined || boundedInteger(value.contextMax, 1, MAX_SAFE_NUMBER))
    && (value.contextPercent === undefined || boundedInteger(value.contextPercent, 0, 100))
}

function validOutcome(value) {
  return hasOnlyKeys(value, ["eventId", "profile", "state", "durationSec", "finishedAt"])
    && boundedText(value.eventId, 128, false)
    && validProfile(value.profile)
    && TERMINAL_STATES.indexOf(value.state) !== -1
    && finiteNumber(value.durationSec, 0, MAX_SAFE_NUMBER)
    && finiteNumber(value.finishedAt, 0, MAX_SAFE_NUMBER)
}

function validRecentSession(value) {
  return hasOnlyKeys(value, ["profile", "sessionId", "description", "recentAt", "avatarUrl"])
    && validProfile(value.profile)
    && typeof value.sessionId === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value.sessionId)
    && boundedText(value.description, 160, false)
    && finiteNumber(value.recentAt, 0, MAX_SAFE_NUMBER)
    && validAvatar(value.avatarUrl)
}

function validatedArray(value, limit, validator) {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.length > limit) throw new Error("invalid snapshot array")
  for (var i = 0; i < value.length; i++) if (!validator(value[i])) throw new Error("invalid snapshot item")
  return value
}

function parseSnapshot(raw) {
  var sourceText = String(raw || "")
  if (utf8ByteLength(sourceText) > MAX_SNAPSHOT_CHARS) {
    var oversized = emptySnapshot()
    oversized.ok = false
    oversized.lastError = "Failed to parse Hermes Watcher status"
    return oversized
  }
  var text = sourceText.trim()
  if (text === "") {
    var empty = emptySnapshot()
    empty.ok = false
    empty.lastError = "Hermes Watcher returned empty status"
    return empty
  }
  try {
    var value = JSON.parse(text)
    if (!value || value.schemaVersion !== 1) throw new Error("unsupported snapshot")
    var allowedTop = ["schemaVersion", "generatedAt", "hermesRoot", "activeBotCount",
      "activeTurnCount", "onlineBotCount", "onlineSessionCount", "onlineProfiles",
      "availableProfiles", "profiles", "recent", "recentSessions", "pendingNotifications",
      "notificationError"]
    if (!hasOnlyKeys(value, allowedTop)) throw new Error("unknown snapshot field")
    var result = emptySnapshot()
    result.ok = true
    result.schemaVersion = 1
    result.generatedAt = value.generatedAt === undefined ? 0 : value.generatedAt
    if (!finiteNumber(result.generatedAt, 0, MAX_SAFE_NUMBER)) throw new Error("invalid timestamp")
    var countKeys = ["activeBotCount", "activeTurnCount", "onlineBotCount", "onlineSessionCount"]
    for (var countIndex = 0; countIndex < countKeys.length; countIndex++) {
      var countKey = countKeys[countIndex]
      var count = value[countKey] === undefined ? 0 : value[countKey]
      if (!boundedInteger(count, 0, 100000)) throw new Error("invalid count")
      result[countKey] = count
    }
    result.hermesRoot = value.hermesRoot === undefined ? "" : value.hermesRoot
    if (result.hermesRoot !== ""
        && (!boundedText(result.hermesRoot, 4096, false) || result.hermesRoot.charAt(0) !== "/"))
      throw new Error("invalid Hermes root")
    result.onlineProfiles = validatedArray(value.onlineProfiles, 100, validOnlineProfile)
    result.availableProfiles = validatedArray(value.availableProfiles, 100, validAvailableProfile)
    result.profiles = validatedArray(value.profiles, 100, validProfileSummary)
    result.recent = validatedArray(value.recent, 100, validOutcome)
    result.recentSessions = validatedArray(value.recentSessions, 100, validRecentSession).slice(0, 6)
    result.pendingNotifications = validatedArray(value.pendingNotifications, 100, validOutcome)
    result.notificationError = value.notificationError === undefined ? "" : value.notificationError
    if (!boundedText(result.notificationError, 200, true)) throw new Error("invalid notification error")
    result.lastError = ""
    return result
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
    utf8ByteLength: utf8ByteLength,
    formatDuration: formatDuration,
    formatRelative: formatRelative,
    formatTokenCount: formatTokenCount,
    notificationText: notificationText,
    stateGlyph: stateGlyph,
    stateLabel: stateLabel,
    filterRecent: filterRecent
  }
}
