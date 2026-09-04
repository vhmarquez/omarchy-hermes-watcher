import QtQuick
import Quickshell
import "." as Plugin

ShellRoot {
  id: root

  Plugin.Service {
    id: service
    autoStart: false
  }

  Timer {
    interval: 0
    running: true
    repeat: false
    onTriggered: {
      service.setupReady = true
      service.reconciledProfilesKey = "default"
      service.startSetup(true)
      if (!service.setupReconciliation) {
        console.error("HERMES_WATCHER_RECONCILE_RUNTIME_FAIL: setup did not start")
      } else {
        service.reconcileProfiles([{ profile: "coder" }])
        if (service.reconciledProfilesKey === "default")
          console.log("HERMES_WATCHER_RECONCILE_RUNTIME_PASS")
        else
          console.error("HERMES_WATCHER_RECONCILE_RUNTIME_FAIL: busy key was recorded")
      }
      Qt.quit()
    }
  }
}
