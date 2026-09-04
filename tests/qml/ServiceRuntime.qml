import QtQuick
import Quickshell
import "." as Plugin

ShellRoot {
  id: root
  property bool launchStarted: false
  property bool completed: false

  Plugin.Service {
    id: service
    autoStart: false
  }

  Timer {
    interval: 0
    running: true
    repeat: false
    onTriggered: {
      var failed = service.parseFailedSetupProfiles(
        "warning\nHERMES_WATCHER_SETUP_FAILED_PROFILES=default,coder\n"
      )
      if (failed.length !== 2 || failed[0] !== "default" || failed[1] !== "coder") {
        console.error("HERMES_WATCHER_QML_RUNTIME_FAIL: setup diagnostics")
        root.completed = true
        Qt.quit()
        return
      }
      service.status = ({ hermesRoot: "/tmp" })
      root.launchStarted = service.launchHermes(["false"])
      if (!root.launchStarted) {
        console.error("HERMES_WATCHER_QML_RUNTIME_FAIL: launch did not start")
        root.completed = true
        Qt.quit()
      }
    }
  }

  Connections {
    target: service
    function onActionInProgressChanged() {
      if (!root.launchStarted || service.actionInProgress || root.completed) return
      root.completed = true
      if (service.actionError === "Could not open a Hermes terminal")
        console.log("HERMES_WATCHER_QML_RUNTIME_PASS")
      else
        console.error("HERMES_WATCHER_QML_RUNTIME_FAIL: " + service.actionError)
      Qt.quit()
    }
  }

  Timer {
    interval: 3000
    running: true
    repeat: false
    onTriggered: {
      if (root.completed) return
      root.completed = true
      console.error("HERMES_WATCHER_QML_RUNTIME_FAIL: timeout")
      Qt.quit()
    }
  }
}
