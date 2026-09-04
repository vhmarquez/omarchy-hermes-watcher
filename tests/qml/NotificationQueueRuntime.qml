import QtQuick
import Quickshell
import "." as Plugin

ShellRoot {
  Plugin.Service {
    id: service
    autoStart: false
  }

  Timer {
    interval: 1
    running: true
    repeat: false
    onTriggered: {
      service.notificationQueue = [
        ({ eventId: "success", profile: "default", state: "succeeded", durationSec: 1, finishedAt: 1 }),
        ({ eventId: "failure", profile: "default", state: "failed", durationSec: 1, finishedAt: 2 })
      ]
      service.attemptedNotifications = ({ success: true, failure: true })
      service.retryNotification = service.notificationQueue[0]
      service.compactNotificationState(({
        recent: [],
        pendingNotifications: [
          ({ eventId: "failure", profile: "default", state: "failed", durationSec: 1, finishedAt: 2 })
        ]
      }))
      service.notificationEligibilityPending = true
      service.startNextNotification()
      if (service.notificationQueue.length === 1
          && service.notificationQueue[0].eventId === "failure"
          && service.retryNotification === null
          && service.currentNotification === null
          && service.attemptedNotifications.success === undefined
          && service.attemptedNotifications.failure === true) {
        console.log("HERMES_WATCHER_NOTIFICATION_FILTER_RUNTIME_PASS")
      } else {
        console.error("HERMES_WATCHER_NOTIFICATION_FILTER_RUNTIME_FAIL")
      }
      Qt.quit()
    }
  }
}
