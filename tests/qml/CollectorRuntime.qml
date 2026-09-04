import QtQuick
import Quickshell
import "." as Plugin

ShellRoot {
  id: root
  property int phase: 0

  Plugin.Service {
    id: service
    autoStart: false
    settings: ({ pollIntervalSec: 2 })
    onStatusChanged: {
      if (!service.hasSnapshot)
        return
      if (root.phase === 0 && Number(service.status.generatedAt) === 1) {
        root.phase = 1
        service.refresh(true)
        resultTimer.restart()
      } else if (root.phase === 1 && Number(service.status.generatedAt) === 2) {
        root.phase = 2
        console.log("HERMES_WATCHER_COLLECTOR_RUNTIME_PASS")
        Qt.quit()
      }
    }
  }

  Timer {
    interval: 0
    running: true
    repeat: false
    onTriggered: {
      service.setupReady = true
      if (typeof service.startCollector !== "function") {
        console.error("HERMES_WATCHER_COLLECTOR_RUNTIME_FAIL missing collector")
        Qt.quit()
        return
      }
      service.startCollector()
    }
  }

  Timer {
    id: resultTimer
    interval: 1500
    repeat: false
    onTriggered: {
      console.error("HERMES_WATCHER_COLLECTOR_RUNTIME_FAIL", root.phase)
      Qt.quit()
    }
  }

  Timer {
    interval: 3000
    running: true
    repeat: false
    onTriggered: {
      console.error("HERMES_WATCHER_COLLECTOR_RUNTIME_FAIL timeout", root.phase)
      Qt.quit()
    }
  }
}
