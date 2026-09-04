import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

Item {
  id: root
  property var shell: null
  property var settings: ({})
  property bool autoStart: true
  property var status: Model.emptySnapshot()
  property bool hasSnapshot: false
  property real lastSuccessfulSnapshotAt: 0
  property real statusClockSec: Date.now() / 1000
  property string setupError: ""
  property string statusError: ""
  property string notificationError: ""
  property string acknowledgementError: ""
  property string consumerError: ""
  property string actionError: ""
  property bool actionInProgress: false
  property int setupFailureCount: 0
  property int collectorFailureCount: 0
  property real collectorRetryAfter: 0
  property bool setupReady: false
  property bool setupReconciliation: false
  property var failedSetupProfiles: []
  property string reconciledProfilesKey: ""
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
  readonly property bool refreshing: snapshotProcess.running
  readonly property string lastError: actionError || setupError || statusError
    || notificationError || acknowledgementError || consumerError

  signal launchSucceeded()

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
  readonly property var recentSessions: status.recentSessions || []
  readonly property int maxNotificationQueueDepth: 100

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
    var recentLimit = Math.min(6, Math.max(1, Number(setting("historyLimit", 6))))
    return ["timeout", "5s", "python3", root.collectorPath, "snapshot",
      "--history-limit", String(recentLimit),
      "--stale-grace-sec", String(setting("staleGraceSec", 30)),
      "--min-duration-sec", String(setting("notifyMinDurationSec", 5)),
      "--max-catchup-age-sec", String(setting("maxCatchupAgeSec", 3600)),
      "--profile-filter", String(setting("profileFilter", "")),
      "--notify-states", setting("notificationsEnabled", true) ? notificationStates() : ""]
  }

  function profileSetKey(profiles) {
    var names = []
    var values = Array.isArray(profiles) ? profiles : []
    for (var i = 0; i < values.length; i++) {
      var profile = String((values[i] && values[i].profile) || "")
      if (profile !== "") names.push(profile)
    }
    names.sort()
    return names.join("\u0000")
  }

  function parseFailedSetupProfiles(stderrText) {
    var prefix = "HERMES_WATCHER_SETUP_FAILED_PROFILES="
    var lines = String(stderrText || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf(prefix) !== 0) continue
      var values = lines[i].slice(prefix.length).split(",")
      var profiles = []
      for (var j = 0; j < values.length; j++) {
        if (/^(default|[a-z0-9][a-z0-9_-]{0,63})$/.test(values[j])) profiles.push(values[j])
      }
      return profiles
    }
    return []
  }

  function reconcileProfiles(profiles) {
    var key = profileSetKey(profiles)
    if (key === reconciledProfilesKey || setupProcess.running) return
    reconciledProfilesKey = key
    root.startSetup(true)
  }

  function startSetup(reconcile) {
    if (!root.setupReady && !setupProcess.running) {
      setupReconciliation = false
      setupProcess.command = ["timeout", "30s", root.setupScriptPath]
      setupProcess.running = true
      return
    }
    if (reconcile === true && !setupProcess.running) {
      setupReconciliation = true
      setupProcess.command = ["timeout", "30s", root.setupScriptPath]
      setupProcess.running = true
    }
  }

  function refresh(force) {
    if (!root.setupReady) {
      root.startSetup()
      return
    }
    if (force !== true && Date.now() < collectorRetryAfter) return
    if (!snapshotProcess.running) {
      snapshotProcess.command = snapshotCommand()
      snapshotProcess.running = true
    }
  }

  function launchHermes(command) {
    var hermesRoot = String(root.status.hermesRoot || "")
    if (hermesRoot.charAt(0) !== "/" || hermesRoot.indexOf("\u0000") !== -1) {
      root.actionError = "Cannot launch Hermes from an invalid data root"
      return false
    }
    if (actionInProgress || launchProcess.running) {
      actionError = "Another Hermes launch is already in progress"
      return false
    }
    root.actionError = ""
    actionInProgress = true
    var terminalCommand = ["omarchy", "launch", "terminal", "env", "HERMES_HOME=" + hermesRoot].concat(command)
    launchProcess.command = ["timeout", "10s"].concat(terminalCommand)
    launchProcess.running = true
    return true
  }

  function launchProfile(profile) {
    profile = String(profile || "")
    var reserved = ["hermes", "test", "tmp", "root", "sudo"]
    if (profile !== "default"
        && (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile) || reserved.indexOf(profile) !== -1)) {
      root.actionError = "Cannot launch an invalid Hermes profile"
      return false
    }
    return root.launchHermes(["hermes", "--profile", profile])
  }

  function resumeSession(profile, sessionId) {
    profile = String(profile || "")
    sessionId = String(sessionId || "")
    var reserved = ["hermes", "test", "tmp", "root", "sudo"]
    if (profile !== "default"
        && (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile) || reserved.indexOf(profile) !== -1)) {
      root.actionError = "Cannot resume a session for an invalid Hermes profile"
      return false
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(sessionId)) {
      root.actionError = "Cannot resume an invalid Hermes session"
      return false
    }
    return root.launchHermes(["hermes", "--profile", profile, "--resume", sessionId])
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
      recordCollectorFailure(parsed.lastError || "Hermes Watcher status unavailable")
      return
    }
    status = parsed
    hasSnapshot = true
    lastSuccessfulSnapshotAt = Number(parsed.generatedAt || Date.now() / 1000)
    collectorFailureCount = 0
    collectorRetryAfter = 0
    statusError = ""
    consumerError = String(parsed.notificationError || "")
    reconcileProfiles(parsed.availableProfiles || [])
    compactNotificationState(parsed)
    if (!setting("notificationsEnabled", true)) return
    var pending = parsed.pendingNotifications || []
    var queue = notificationQueue.slice()
    var now = Date.now()
    for (var i = 0; i < pending.length; i++) {
      var event = pending[i]
      if (!event || attemptedNotifications[event.eventId]) continue
      if (Number(notificationRetryAfter[event.eventId] || 0) > now) continue
      if (queue.length >= maxNotificationQueueDepth) break
      attemptedNotifications[event.eventId] = true
      queue.push(event)
    }
    attemptedNotifications = Object.assign({}, attemptedNotifications)
    notificationQueue = queue
    startNextNotification()
  }

  function finishSnapshot(exitCode, exitStatus, raw) {
    if (exitCode !== 0 || exitStatus !== 0) {
      recordCollectorFailure("Hermes Watcher collector failed")
      return
    }
    applySnapshot(raw)
  }

  function recordCollectorFailure(message) {
    collectorFailureCount += 1
    var delay = Math.min(60000, 1000 * Math.pow(2, Math.min(collectorFailureCount, 7) - 1))
    collectorRetryAfter = Date.now() + delay
    statusError = message
  }

  function startNextNotification() {
    if (acknowledgementQueue.length >= maxNotificationQueueDepth) return
    if (!setting("notificationsEnabled", true) || retryNotification
        || notifyProcess.running || currentNotification || notificationQueue.length === 0) return
    var queue = notificationQueue.slice()
    currentNotification = queue.shift()
    notificationQueue = queue
    var text = Model.notificationText(currentNotification)
    notifyProcess.command = ["timeout", "15s", "python3", root.collectorPath, "deliver-notification",
      String(currentNotification.eventId), "--icon", root.notificationIconPath,
      "--title", text.title, "--body", text.body]
    notifyProcess.running = true
  }

  function enqueueAcknowledgement(eventId) {
    var id = String(eventId || "")
    if (id === "" || id === ackEventId || acknowledgementQueue.indexOf(id) !== -1) return
    var queue = acknowledgementQueue.slice()
    queue.push(id)
    acknowledgementQueue = queue
    startAcknowledgement()
  }

  function finishNotificationResult(exitCode) {
    if (!currentNotification) return
    if (exitCode === 75) {
      var eventId = String(currentNotification.eventId)
      currentNotification = null
      delete attemptedNotifications[eventId]
      attemptedNotifications = Object.assign({}, attemptedNotifications)
      notificationError = ""
      startNextNotification()
      return
    }
    finishNotification(exitCode === 0)
  }

  function finishNotification(delivered) {
    if (!currentNotification) return
    var eventId = String(currentNotification.eventId)
    if (delivered) {
      notificationError = ""
      currentNotification = null
      delete attemptedNotifications[eventId]
      delete notificationAttempts[eventId]
      delete notificationRetryAfter[eventId]
      attemptedNotifications = Object.assign({}, attemptedNotifications)
      notificationAttempts = Object.assign({}, notificationAttempts)
      notificationRetryAfter = Object.assign({}, notificationRetryAfter)
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
      notificationError = "Notification delivery failed; retrying later"
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
    notificationError = "Notification delivery failed; retrying"
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
    ackProcess.command = ["timeout", "5s", "python3", root.collectorPath, "acknowledge", ackEventId]
    ackProcess.running = true
  }

  function finishAcknowledgement(exitCode) {
    var eventId = ackEventId
    ackEventId = ""
    if (eventId === "") return
    if (exitCode === 0) {
      acknowledgementError = ""
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
      acknowledgementError = "Notification acknowledgement failed; retrying in background"
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
    interval: 1000
    running: root.hasSnapshot && root.statusError !== ""
    repeat: true
    triggeredOnStart: true
    onTriggered: root.statusClockSec = Date.now() / 1000
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
      pruneProcess.command = ["timeout", "30s", "python3", root.collectorPath, "prune", "--keep-terminal", "100"]
      pruneProcess.running = true
    }
  }

  Timer {
    id: setupRetryTimer
    interval: 30000
    repeat: false
    onTriggered: root.startSetup(root.setupReady)
  }

  Process {
    id: setupProcess
    command: ["timeout", "30s", root.setupScriptPath]
    running: root.autoStart
    onExited: function(exitCode, exitStatus) {
      var wasReconciliation = root.setupReconciliation
      var succeeded = exitCode === 0 && exitStatus === 0
      var partial = setupStderr.text.indexOf("completed with profile warnings") !== -1
      root.failedSetupProfiles = root.parseFailedSetupProfiles(setupStderr.text)
      root.setupReconciliation = false
      if (succeeded) {
        root.setupReady = true
        if (partial) {
          root.setupFailureCount += 1
          root.setupError = failedSetupProfiles.length > 0
            ? "Could not instrument Hermes profiles: " + failedSetupProfiles.join(", ")
            : "Some Hermes profiles could not be instrumented"
          setupRetryTimer.interval = Math.min(300000, 1000 * Math.pow(2,
            Math.min(root.setupFailureCount, 9) - 1))
          setupRetryTimer.restart()
        } else {
          root.setupFailureCount = 0
          root.failedSetupProfiles = []
          root.setupError = ""
          setupRetryTimer.stop()
        }
        root.refresh()
      } else {
        if (!wasReconciliation) root.setupReady = false
        root.setupFailureCount += 1
        root.setupError = "Hermes observer setup failed"
        setupRetryTimer.interval = Math.min(300000, 1000 * Math.pow(2,
          Math.min(root.setupFailureCount, 9) - 1))
        setupRetryTimer.restart()
      }
    }
    stderr: StdioCollector {
      id: setupStderr
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("vhm.hermes-bots/setup", text.trim())
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
    command: ["timeout", "5s", "python3", root.collectorPath, "snapshot"]
    stdout: StdioCollector {
      id: snapshotStdout
      waitForEnd: true
    }
    onExited: function(exitCode, exitStatus) { root.finishSnapshot(exitCode, exitStatus, snapshotStdout.text) }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("vhm.hermes-bots/snapshot", text.trim())
    }
  }

  Process {
    id: launchProcess
    running: false
    onExited: function(exitCode, exitStatus) {
      if (exitCode === 0 && exitStatus === 0) {
        root.actionError = ""
        root.actionInProgress = false
        root.launchSucceeded()
      } else {
        root.actionError = "Could not open a Hermes terminal"
        root.actionInProgress = false
      }
    }
  }

  Process {
    id: notifyProcess
    running: false
    onExited: function(exitCode) { root.finishNotificationResult(exitCode) }
  }

  Process {
    id: ackProcess
    running: false
    onExited: function(exitCode) { root.finishAcknowledgement(exitCode) }
  }

  Component.onDestruction: if (root.autoStart) Quickshell.execDetached([root.cleanupScriptPath])

  IpcHandler {
    target: "vhm.hermes-bots"
    function refresh(): string { root.refresh(true); return "ok" }
    function status(): string { return JSON.stringify(root.status) }
    function notificationsOn(): string { return root.setNotificationsEnabled(true) }
    function notificationsOff(): string { return root.setNotificationsEnabled(false) }
    function notificationsToggle(): string {
      return root.setNotificationsEnabled(!root.setting("notificationsEnabled", true))
    }
  }
}
