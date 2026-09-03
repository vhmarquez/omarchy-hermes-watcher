import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

Item {
  id: root
  property var shell: null
  property var settings: ({})
  property var status: Model.emptySnapshot()
  property string lastError: ""
  property bool setupReady: false
  property var notificationQueue: []
  property var attemptedNotifications: ({})
  property var notificationRetryAfter: ({})
  property var notificationAttempts: ({})
  property var currentNotification: null
  property var retryNotification: null
  property var acknowledgementQueue: []
  property var acknowledgementAttempts: ({})
  property var acknowledgementRetryAfter: ({})
  property string ackEventId: ""

  readonly property string collectorPath: decodeURIComponent(Qt.resolvedUrl("hermes_bot_status.py").toString().replace(/^file:\/\//, ""))
  readonly property string setupScriptPath: decodeURIComponent(Qt.resolvedUrl("scripts/setup-profiles").toString().replace(/^file:\/\//, ""))
  readonly property string cleanupScriptPath: decodeURIComponent(Qt.resolvedUrl("scripts/cleanup-observer").toString().replace(/^file:\/\//, ""))
  readonly property string notificationIconPath: decodeURIComponent(Qt.resolvedUrl("assets/hermes-watcher.svg").toString().replace(/^file:\/\//, ""))
  readonly property int activeBotCount: Number(status.activeBotCount || 0)
  readonly property int activeTurnCount: Number(status.activeTurnCount || 0)
  readonly property int onlineBotCount: Number(status.onlineBotCount || 0)
  readonly property int onlineSessionCount: Number(status.onlineSessionCount || 0)
  readonly property var onlineProfiles: status.onlineProfiles || []
  readonly property var availableProfiles: status.availableProfiles || []
  readonly property var profiles: status.profiles || []
  readonly property var recent: status.recent || []

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function notificationStates() {
    var states = []
    if (setting("notifyOnSuccess", true)) states.push("succeeded")
    if (setting("notifyOnFailure", true)) states.push("failed")
    if (setting("notifyOnInterrupted", false)) states.push("interrupted")
    if (setting("notifyOnStale", false)) states.push("stale")
    return states.join(",")
  }

  function snapshotCommand() {
    return ["python3", root.collectorPath, "snapshot",
      "--history-limit", String(setting("historyLimit", 20)),
      "--stale-grace-sec", String(setting("staleGraceSec", 30)),
      "--min-duration-sec", String(setting("notifyMinDurationSec", 5)),
      "--max-catchup-age-sec", String(setting("maxCatchupAgeSec", 3600)),
      "--profile-filter", String(setting("profileFilter", "")),
      "--notify-states", setting("notificationsEnabled", true) ? notificationStates() : ""]
  }

  function startSetup() {
    if (!root.setupReady && !setupProcess.running) {
      setupProcess.command = [root.setupScriptPath]
      setupProcess.running = true
    }
  }

  function refresh() {
    if (!root.setupReady) {
      root.startSetup()
      return
    }
    if (!snapshotProcess.running) {
      snapshotProcess.command = snapshotCommand()
      snapshotProcess.running = true
    }
  }

  function launchProfile(profile) {
    profile = String(profile || "")
    var reserved = ["hermes", "test", "tmp", "root", "sudo"]
    if (profile !== "default"
        && (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile) || reserved.indexOf(profile) !== -1)) {
      root.lastError = "Cannot launch an invalid Hermes profile"
      return false
    }
    Quickshell.execDetached(["omarchy", "launch", "terminal", "hermes", "--profile", profile])
    return true
  }

  function addEventIds(values, valid) {
    for (var i = 0; i < values.length; i++) {
      if (values[i] && values[i].eventId) valid[String(values[i].eventId)] = true
    }
  }

  function compactMap(values, valid) {
    var compacted = ({})
    for (var key in values) if (valid[key]) compacted[key] = values[key]
    return compacted
  }

  function compactNotificationState(parsed) {
    var valid = ({})
    addEventIds(parsed.recent || [], valid)
    addEventIds(parsed.pendingNotifications || [], valid)
    addEventIds(notificationQueue || [], valid)
    if (currentNotification && currentNotification.eventId) valid[String(currentNotification.eventId)] = true
    if (retryNotification && retryNotification.eventId) valid[String(retryNotification.eventId)] = true
    for (var i = 0; i < acknowledgementQueue.length; i++) valid[String(acknowledgementQueue[i])] = true
    if (ackEventId !== "") valid[ackEventId] = true
    attemptedNotifications = compactMap(attemptedNotifications, valid)
    notificationRetryAfter = compactMap(notificationRetryAfter, valid)
    notificationAttempts = compactMap(notificationAttempts, valid)
    acknowledgementRetryAfter = compactMap(acknowledgementRetryAfter, valid)
    acknowledgementAttempts = compactMap(acknowledgementAttempts, valid)
  }

  function applySnapshot(raw) {
    var parsed = Model.parseSnapshot(raw)
    if (!parsed.ok) {
      lastError = parsed.lastError || "Hermes Watcher status unavailable"
      return
    }
    status = parsed
    lastError = ""
    compactNotificationState(parsed)
    if (!setting("notificationsEnabled", true)) return
    var pending = parsed.pendingNotifications || []
    var queue = notificationQueue.slice()
    var now = Date.now()
    for (var i = 0; i < pending.length; i++) {
      var event = pending[i]
      if (!event || attemptedNotifications[event.eventId]) continue
      if (Number(notificationRetryAfter[event.eventId] || 0) > now) continue
      attemptedNotifications[event.eventId] = true
      queue.push(event)
    }
    attemptedNotifications = Object.assign({}, attemptedNotifications)
    notificationQueue = queue
    startNextNotification()
  }

  function finishSnapshot(exitCode, exitStatus, raw) {
    if (exitCode !== 0 || exitStatus !== 0) {
      lastError = "Hermes Watcher collector failed"
      return
    }
    applySnapshot(raw)
  }

  function startNextNotification() {
    if (!setting("notificationsEnabled", true) || retryNotification
        || notifyProcess.running || currentNotification || notificationQueue.length === 0) return
    var queue = notificationQueue.slice()
    currentNotification = queue.shift()
    notificationQueue = queue
    var text = Model.notificationText(currentNotification)
    notifyProcess.command = ["notify-send", "--app-name=Hermes Watcher", "--urgency=normal",
      "--icon=" + root.notificationIconPath, text.title, text.body]
    notifyProcess.running = true
  }

  function enqueueAcknowledgement(eventId) {
    var id = String(eventId || "")
    if (id === "" || id === ackEventId || acknowledgementQueue.indexOf(id) !== -1) return
    var queue = acknowledgementQueue.slice(-99)
    queue.push(id)
    acknowledgementQueue = queue
    startAcknowledgement()
  }

  function finishNotification(delivered) {
    if (!currentNotification) return
    var eventId = String(currentNotification.eventId)
    if (delivered) {
      currentNotification = null
      enqueueAcknowledgement(eventId)
      startNextNotification()
      return
    }
    var attempts = Number(notificationAttempts[eventId] || 0) + 1
    notificationAttempts[eventId] = attempts
    notificationAttempts = Object.assign({}, notificationAttempts)
    currentNotification = null
    if (attempts >= 3) {
      notificationAttempts[eventId] = 0
      notificationRetryAfter[eventId] = Date.now() + 300000
      delete attemptedNotifications[eventId]
      attemptedNotifications = Object.assign({}, attemptedNotifications)
      notificationRetryAfter = Object.assign({}, notificationRetryAfter)
      lastError = "Notification delivery failed; retrying later"
      startNextNotification()
      return
    }
    var delay = Math.min(60000, 1000 * Math.pow(2, attempts - 1))
    notificationRetryAfter[eventId] = Date.now() + delay
    notificationRetryAfter = Object.assign({}, notificationRetryAfter)
    retryNotification = ({ eventId: eventId })
    for (var key in status.pendingNotifications || []) {
      var candidate = status.pendingNotifications[key]
      if (candidate && String(candidate.eventId) === eventId) retryNotification = candidate
    }
    lastError = "Notification delivery failed; retrying"
    notificationRetryTimer.interval = delay
    notificationRetryTimer.restart()
  }

  function startAcknowledgement() {
    if (ackProcess.running || ackEventId !== "" || acknowledgementQueue.length === 0) return
    var queue = acknowledgementQueue.slice()
    var now = Date.now()
    var selected = -1
    for (var i = 0; i < queue.length; i++) {
      if (Number(acknowledgementRetryAfter[queue[i]] || 0) <= now) {
        selected = i
        break
      }
    }
    if (selected < 0) return
    ackEventId = String(queue.splice(selected, 1)[0])
    acknowledgementQueue = queue
    ackProcess.command = ["python3", root.collectorPath, "acknowledge", ackEventId]
    ackProcess.running = true
  }

  function finishAcknowledgement(exitCode) {
    var eventId = ackEventId
    ackEventId = ""
    if (eventId === "") return
    if (exitCode === 0) {
      delete attemptedNotifications[eventId]
      delete notificationAttempts[eventId]
      delete notificationRetryAfter[eventId]
      delete acknowledgementAttempts[eventId]
      delete acknowledgementRetryAfter[eventId]
      attemptedNotifications = Object.assign({}, attemptedNotifications)
      notificationAttempts = Object.assign({}, notificationAttempts)
      notificationRetryAfter = Object.assign({}, notificationRetryAfter)
      acknowledgementAttempts = Object.assign({}, acknowledgementAttempts)
      acknowledgementRetryAfter = Object.assign({}, acknowledgementRetryAfter)
    } else {
      var attempts = Number(acknowledgementAttempts[eventId] || 0) + 1
      acknowledgementAttempts[eventId] = attempts
      acknowledgementRetryAfter[eventId] = Date.now() + Math.min(60000, 1000 * Math.pow(2, Math.min(attempts, 6) - 1))
      acknowledgementAttempts = Object.assign({}, acknowledgementAttempts)
      acknowledgementRetryAfter = Object.assign({}, acknowledgementRetryAfter)
      var queue = acknowledgementQueue.slice()
      if (queue.indexOf(eventId) === -1) queue.push(eventId)
      acknowledgementQueue = queue
      lastError = "Notification acknowledgement failed; retrying in background"
    }
    startAcknowledgement()
    startNextNotification()
  }

  function setNotificationsEnabled(enabled) {
    var entry = { id: "vhm.hermes-bots" }
    for (var key in settings) if (key !== "id") entry[key] = settings[key]
    entry.notificationsEnabled = enabled === true
    settings = entry
    if (!entry.notificationsEnabled) {
      for (var i = 0; i < notificationQueue.length; i++) {
        if (notificationQueue[i] && notificationQueue[i].eventId)
          delete attemptedNotifications[String(notificationQueue[i].eventId)]
      }
      notificationQueue = []
      if (retryNotification) {
        delete attemptedNotifications[String(retryNotification.eventId)]
        retryNotification = null
        notificationRetryTimer.stop()
      }
      attemptedNotifications = Object.assign({}, attemptedNotifications)
    }
    if (shell && typeof shell.updateEntryInline === "function")
      shell.updateEntryInline("vhm.hermes-bots", entry)
    return entry.notificationsEnabled ? "on" : "off"
  }

  Timer {
    interval: Math.max(1, Number(root.setting("pollIntervalSec", 2))) * 1000
    running: root.setupReady
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: notificationRetryTimer
    repeat: false
    onTriggered: {
      if (!root.retryNotification) return
      if (!root.setting("notificationsEnabled", true)) {
        delete root.attemptedNotifications[String(root.retryNotification.eventId)]
        root.attemptedNotifications = Object.assign({}, root.attemptedNotifications)
        root.retryNotification = null
        return
      }
      var queue = root.notificationQueue.slice()
      queue.unshift(root.retryNotification)
      root.retryNotification = null
      root.notificationQueue = queue
      root.startNextNotification()
    }
  }

  Timer {
    id: ackRetryTimer
    interval: 1000
    repeat: true
    running: root.acknowledgementQueue.length > 0
    onTriggered: root.startAcknowledgement()
  }

  Timer {
    interval: 3600000
    running: root.setupReady
    repeat: true
    onTriggered: if (!pruneProcess.running) {
      pruneProcess.command = ["python3", root.collectorPath, "prune", "--keep-terminal", "100"]
      pruneProcess.running = true
    }
  }

  Timer {
    id: setupRetryTimer
    interval: 30000
    repeat: false
    onTriggered: root.startSetup()
  }

  Process {
    id: setupProcess
    command: [root.setupScriptPath]
    running: true
    onExited: function(exitCode, exitStatus) {
      root.setupReady = exitCode === 0 && exitStatus === 0
      if (root.setupReady) {
        setupRetryTimer.stop()
        root.refresh()
      } else {
        root.lastError = "Hermes observer setup failed"
        setupRetryTimer.restart()
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") root.lastError = text.trim()
    }
  }

  Process {
    id: pruneProcess
    running: false
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("vhm.hermes-bots/prune", text.trim())
    }
  }

  Process {
    id: snapshotProcess
    running: false
    command: ["python3", root.collectorPath, "snapshot"]
    stdout: StdioCollector {
      id: snapshotStdout
      waitForEnd: true
    }
    onExited: function(exitCode, exitStatus) { root.finishSnapshot(exitCode, exitStatus, snapshotStdout.text) }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") root.lastError = text.trim()
    }
  }

  Process {
    id: notifyProcess
    running: false
    onExited: function(exitCode) { root.finishNotification(exitCode === 0) }
  }

  Process {
    id: ackProcess
    running: false
    onExited: function(exitCode) { root.finishAcknowledgement(exitCode) }
  }

  Component.onDestruction: Quickshell.execDetached([root.cleanupScriptPath])

  IpcHandler {
    target: "vhm.hermes-bots"
    function refresh(): string { root.refresh(); return "ok" }
    function status(): string { return JSON.stringify(root.status) }
    function notificationsOn(): string { return root.setNotificationsEnabled(true) }
    function notificationsOff(): string { return root.setNotificationsEnabled(false) }
    function notificationsToggle(): string {
      return root.setNotificationsEnabled(!root.setting("notificationsEnabled", true))
    }
  }
}
