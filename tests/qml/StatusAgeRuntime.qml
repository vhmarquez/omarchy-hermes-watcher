import QtQuick
import Quickshell
import "." as Plugin

ShellRoot {
  id: root
  property real initialClock: 0

  Plugin.Service {
    id: service
    autoStart: false
  }

  Timer {
    interval: 0
    running: true
    repeat: false
    onTriggered: {
      service.hasSnapshot = true
      service.statusError = "collector unavailable"
      root.initialClock = service.statusClockSec
      ageCheck.start()
    }
  }

  Timer {
    id: ageCheck
    interval: 1200
    repeat: false
    onTriggered: {
      if (service.statusClockSec > root.initialClock)
        console.log("HERMES_WATCHER_AGE_RUNTIME_PASS")
      else
        console.error("HERMES_WATCHER_AGE_RUNTIME_FAIL")
      Qt.quit()
    }
  }
}
