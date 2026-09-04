import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "vhm.hermes-bots"
  ipcTarget: "vhm.hermes-bots.panel"
  manageIpc: false

  readonly property var monitor: bar && bar.shell ? bar.shell.serviceFor("vhm.hermes-bots") : null
  readonly property var status: monitor ? monitor.status : Model.emptySnapshot()
  readonly property int activeBotCount: Number(status.activeBotCount || 0)
  readonly property int onlineBotCount: Number(status.onlineBotCount || 0)
  readonly property string heroStatusText: {
    if (activeBotCount > 0)
      return activeBotCount + (activeBotCount === 1 ? " Agent working" : " Agents working")
    if (onlineBotCount > 0)
      return onlineBotCount + (onlineBotCount === 1 ? " Agent online" : " Agents online")
    return "No Agents running"
  }
  readonly property bool notificationsEnabled: !(root.settings && root.settings.notificationsEnabled === false)
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  property bool failureUnseen: false
  property string latestFailureEventId: ""
  property string agentTab: "active"
  property var expandedSessionKeys: ({})

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function syncSettings() {
    if (monitor) monitor.settings = root.settings || ({})
  }

  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) entry[key] = values[key]
    root.settings = entry
    if (monitor) monitor.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function toggleNotifications() {
    var enabled = !(root.settings && root.settings.notificationsEnabled === false)
    if (monitor && typeof monitor.setNotificationsEnabled === "function") {
      monitor.setNotificationsEnabled(!enabled)
      return
    }
    persistSettings({ notificationsEnabled: !enabled })
  }

  function selectAgentTab(value) {
    var next = String(value || "")
    if (next !== "active" && next !== "recent") return
    if (root.agentTab === next) return
    root.agentTab = next
    panelFlick.contentY = 0
  }

  function sessionDescriptionExpanded(sessionKey) {
    var key = String(sessionKey || "")
    return key !== "" && root.expandedSessionKeys[key] === true
  }

  function toggleSessionDescription(sessionKey) {
    var key = String(sessionKey || "")
    if (key === "") return
    var next = ({})
    for (var existing in root.expandedSessionKeys) {
      if (existing !== key && root.expandedSessionKeys[existing] === true)
        next[existing] = true
    }
    if (root.expandedSessionKeys[key] !== true) next[key] = true
    root.expandedSessionKeys = next
  }

  function pruneExpandedSessionKeys() {
    var valid = ({})
    var snapshot = root.status || ({})
    var sessions = snapshot.onlineProfiles || []
    for (var i = 0; i < sessions.length; i++) {
      var key = String((sessions[i] && sessions[i].sessionKey) || "")
      if (key !== "") valid[key] = true
    }
    var next = ({})
    for (var existing in root.expandedSessionKeys) {
      if (valid[existing] === true && root.expandedSessionKeys[existing] === true)
        next[existing] = true
    }
    root.expandedSessionKeys = next
  }

  function updateFailureIndicator() {
    var recent = root.monitor ? (root.monitor.recent || []) : []
    var newest = ""
    for (var i = 0; i < recent.length; i++) {
      if (recent[i] && recent[i].state === "failed") {
        newest = String(recent[i].eventId || "")
        break
      }
    }
    latestFailureEventId = newest
    failureUnseen = !root.opened && newest !== ""
      && newest !== String((root.settings && root.settings.seenFailureEventId) || "")
  }

  function markFailureSeen() {
    if (latestFailureEventId === "") {
      failureUnseen = false
      return
    }
    persistSettings({ seenFailureEventId: latestFailureEventId })
    failureUnseen = false
  }

  onSettingsChanged: {
    syncSettings()
    updateFailureIndicator()
  }
  onMonitorChanged: {
    syncSettings()
    updateFailureIndicator()
    pruneExpandedSessionKeys()
  }
  onOpenedChanged: if (opened) markFailureSeen()

  Connections {
    target: monitor
    function onStatusChanged() {
      root.updateFailureIndicator()
      root.pruneExpandedSessionKeys()
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: "Hermes Watcher"
    active: root.onlineBotCount > 0 || root.failureUnseen
    iconComponent: Component {
      Item {
        id: barEmblem

        Text {
          id: officialFavicon
          anchors.centerIn: parent
          textFormat: Text.PlainText
          text: "\u2695\uFE0E"
          color: root.foreground
          font.family: "Noto Sans Symbols"
          font.pixelSize: Math.round(button.fontSize * 1.08)
          renderType: Text.NativeRendering
          SequentialAnimation on opacity {
            running: root.activeBotCount > 0
            loops: Animation.Infinite
            NumberAnimation { to: 0.55; duration: 650 }
            NumberAnimation { to: 1.0; duration: 650 }
          }
        }
        Text {
          id: countText
          visible: root.onlineBotCount > 0
          anchors.right: parent.right
          anchors.top: parent.top
          anchors.rightMargin: -Style.spacing.sm
          anchors.topMargin: -Style.spacing.xxs
          textFormat: Text.PlainText
          text: root.onlineBotCount > 9 ? "9+" : String(root.onlineBotCount)
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Math.max(1, Style.font.caption - Style.spacing.xxs)
          font.bold: true
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) {
        root.toggleNotifications()
      } else if (buttonCode === Qt.RightButton) {
        if (root.monitor) root.monitor.refresh()
      } else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onMoveRequested: function(dx, dy) {
        if (dx < 0) root.selectAgentTab("active")
        else if (dx > 0) root.selectAgentTab("recent")
        if (dy !== 0) {
          var limit = Math.max(0, panelFlick.contentHeight - panelFlick.height)
          panelFlick.contentY = Math.max(0, Math.min(limit,
            panelFlick.contentY + dy * Style.space(52)))
        }
      }
      onTextKey: function(text) {
        if (text === "r" || text === "R") {
          if (root.monitor) root.monitor.refresh()
        } else if (text === "n" || text === "N") {
          root.toggleNotifications()
        }
      }

      Flickable {
      id: panelFlick
      anchors.fill: parent
      contentWidth: width
      contentHeight: content.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      interactive: contentHeight > height
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      Column {
        id: content
        width: panelFlick.width
        spacing: Style.space(14)

      // ---------- Hero: Hermes Watcher icon · status ----------
      Item {
        width: parent.width
        implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, notificationSwitch.implicitHeight)

        // Use the same Nerd Font line box as Bluetooth for layout. The official
        // Hermes mark uses different font metrics, so keep those metrics from
        // inflating this header at the same nominal pixel size.
        Text {
          id: bluetoothIconReference
          visible: false
          textFormat: Text.PlainText
          text: "󰂯"
          font.family: root.fontFamily
          font.pixelSize: Style.font.display
        }

        Item {
          id: heroIcon
          implicitWidth: bluetoothIconReference.implicitWidth
          implicitHeight: bluetoothIconReference.implicitHeight
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter

          Text {
            anchors.centerIn: parent
            anchors.verticalCenterOffset: -Style.space(5)
            textFormat: Text.PlainText
            text: "\u2695\uFE0E"
            color: root.foreground
            font.family: "Noto Sans Symbols"
            // Match Bluetooth's painted icon height, not just its nominal font size.
            font.pixelSize: Math.round(Style.font.display * 1.55)
          }
        }

        ToggleSwitch {
          id: notificationSwitch
          checked: root.notificationsEnabled
          foreground: root.foreground
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          onToggled: root.toggleNotifications()

          PanelToolTip {
            visible: notificationSwitch.containsMouse
            text: root.notificationsEnabled ? "Turn notifications off" : "Turn notifications on"
            fontFamily: root.fontFamily
          }
        }

        Column {
          id: heroLabels
          anchors.left: heroIcon.right
          anchors.leftMargin: Style.space(14)
          anchors.right: parent.right
          anchors.rightMargin: notificationSwitch.width + Style.space(12)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(2)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "Hermes Watcher"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.heroStatusText.toUpperCase()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            elide: Text.ElideRight
          }
        }
      }

      PanelSeparator {
        foreground: root.foreground
      }

      Text {
        visible: root.monitor && root.monitor.lastError !== ""
        width: parent.width
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        text: root.monitor ? root.monitor.lastError : ""
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      PanelSectionHeader {
        text: "AGENTS"
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      Flickable {
        id: profileLauncherFlick
        visible: (root.status.availableProfiles || []).length > 0
        width: parent.width
        height: profileLauncherRow.implicitHeight
        contentWidth: profileLauncherRow.implicitWidth
        contentHeight: height
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.HorizontalFlick
        interactive: contentWidth > width
        ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }

        Row {
          id: profileLauncherRow
          spacing: Style.space(6)

          Repeater {
            model: root.status.availableProfiles || []

            Item {
              id: profileLauncher
              required property var modelData
              width: Style.space(36)
              height: Style.space(36)

              Image {
                anchors.centerIn: parent
                width: Style.space(30)
                height: Style.space(30)
                source: modelData.avatarUrl
                  ? String(modelData.avatarUrl)
                  : Qt.resolvedUrl("assets/hermes-watcher.svg")
                sourceSize.width: width * Screen.devicePixelRatio
                sourceSize.height: height * Screen.devicePixelRatio
                fillMode: Image.PreserveAspectFit
                asynchronous: true
              }

              MouseArea {
                id: profileLauncherMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                  if (root.monitor && root.monitor.launchProfile(String(modelData.profile || "")))
                    root.close()
                }
              }

              PanelToolTip {
                visible: profileLauncherMouse.containsMouse
                text: String(modelData.profile || "Hermes")
                fontFamily: root.fontFamily
              }
            }
          }
        }
      }

      Item {
        width: parent.width
        implicitHeight: agentTabs.implicitHeight

        ButtonGroup {
          id: agentTabs
          anchors.horizontalCenter: parent.horizontalCenter
          options: [{ value: "active", label: "Active" },
            { value: "recent", label: "Recent" }]
          value: root.agentTab
          foreground: root.foreground
          background: Color.background
          accent: Color.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          focusable: false
          onChanged: function(value) { root.selectAgentTab(value) }
        }
      }

      Column {
        id: activeTabContent
        visible: root.agentTab === "active"
        width: parent.width
        spacing: Style.space(5)
        Repeater {
          model: root.status.onlineProfiles || []
          BorderSurface {
            id: botCard
            required property var modelData
            readonly property string sessionKey: String(modelData.sessionKey || "")
            readonly property bool descriptionExpanded: root.sessionDescriptionExpanded(sessionKey)
            readonly property real cardPadding: Style.space(8)
            readonly property bool isWorking: Number(modelData.activeTurnCount || 0) > 0
            readonly property bool hasContext: Number(modelData.contextUsed || 0) > 0
              && Number(modelData.contextMax || 0) > 0
            readonly property real contextFraction: hasContext
              ? Math.max(0, Math.min(1,
                  Number(modelData.contextUsed || 0) / Number(modelData.contextMax || 1)))
              : 0
            width: parent.width
            implicitHeight: Math.max(cardContent.implicitHeight, agentIcon.height, agentState.implicitHeight) + 2 * cardPadding
            height: implicitHeight
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.foreground, Color.accent)

            TapHandler {
              acceptedButtons: Qt.LeftButton
              onTapped: root.toggleSessionDescription(botCard.sessionKey)
            }

            Image {
              id: agentIcon
              anchors.left: parent.left
              anchors.leftMargin: botCard.cardPadding
              y: botCard.cardPadding
              width: Style.space(24)
              height: Style.space(24)
              source: modelData.avatarUrl
                ? String(modelData.avatarUrl)
                : Qt.resolvedUrl("assets/hermes-watcher.svg")
              sourceSize.width: width * Screen.devicePixelRatio
              sourceSize.height: height * Screen.devicePixelRatio
              fillMode: Image.PreserveAspectFit
              asynchronous: true
            }

            Text {
              id: agentState
              anchors.top: parent.top
              anchors.topMargin: botCard.cardPadding
              anchors.right: parent.right
              anchors.rightMargin: botCard.cardPadding
              textFormat: Text.PlainText
              text: modelData.activeTurnCount > 0 ? "󰔟" : "󰖟"
              color: modelData.activeTurnCount > 0 ? Color.accent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Column {
              id: cardContent
              anchors.left: parent.left
              anchors.leftMargin: Style.space(40)
              anchors.right: parent.right
              anchors.rightMargin: Style.space(32)
              anchors.top: parent.top
              anchors.topMargin: botCard.cardPadding
              spacing: Style.space(1)

              Row {
                id: agentHeader
                width: parent.width
                spacing: Style.space(4)

                Text {
                  id: agentTitle
                  width: Math.min(implicitWidth,
                    Math.max(0, agentHeader.width - sessionTime.implicitWidth - agentHeader.spacing))
                  textFormat: Text.PlainText
                  text: String(modelData.profile || "Hermes Agent")
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                  elide: Text.ElideRight
                }

                Text {
                  id: sessionTime
                  textFormat: Text.PlainText
                  text: "(" + Model.formatDuration(modelData.runningForSec) + ")"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: false
                }
              }
              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: modelData.workDescription
                  ? String(modelData.workDescription)
                  : (modelData.activeTurnCount > 0
                    ? "Current task unavailable"
                    : "Idle — awaiting a task")
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: botCard.descriptionExpanded ? Text.Wrap : Text.NoWrap
                elide: botCard.descriptionExpanded ? Text.ElideNone : Text.ElideRight
              }
              Text {
                visible: botCard.isWorking
                width: parent.width
                textFormat: Text.PlainText
                text: String(modelData.model || "Unknown model") + " · "
                  + String(modelData.platform || "local")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              Item {
                id: contextGauge
                visible: botCard.hasContext
                width: parent.width
                height: Style.space(14)

                Rectangle {
                  id: contextTrack
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  height: Math.max(2, Style.space(4))
                  radius: height / 2
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.16)

                  Rectangle {
                    width: parent.width * Math.min(1, botCard.contextFraction)
                    height: parent.height
                    radius: parent.radius
                    color: Number(modelData.contextPercent || 0) >= 90 ? root.urgent : Color.accent
                  }
                }

                MouseArea {
                  id: contextHover
                  anchors.fill: parent
                  hoverEnabled: true
                  acceptedButtons: Qt.NoButton
                }

                PanelToolTip {
                  visible: contextHover.containsMouse
                  text: "Model: " + String(modelData.model || "Unavailable")
                    + "\n"
                    + "Reasoning: " + String(modelData.reasoningLevel || "Unavailable")
                    + "\n" + (modelData.contextIsLastKnown ? "Last context: " : "Context: ")
                    + Model.formatTokenCount(modelData.contextUsed) + " / "
                    + Model.formatTokenCount(modelData.contextMax) + " ("
                    + Math.round(Number(modelData.contextPercent || 0)) + "%)"
                  fontFamily: root.fontFamily
                }
              }
            }
          }
        }
      }

      BorderSurface {
        visible: root.agentTab === "active" && root.onlineBotCount === 0
        width: parent.width
        height: Style.space(52)
        radius: Style.cornerRadius
        color: Style.normalFillFor(root.foreground, Color.accent)
        Row {
          anchors.fill: parent
          anchors.margins: Style.space(8)
          spacing: Style.space(9)
          Text {
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: "󰄬"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
          Column {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - Style.space(28)
            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: "No Agents running"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }
            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: "Monitoring local Hermes profiles"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      Column {
        id: recentTabContent
        visible: root.agentTab === "recent"
        width: parent.width
        spacing: Style.space(3)

        BorderSurface {
          visible: (root.status.recentSessions || []).length === 0
          width: parent.width
          height: Style.space(52)
          radius: Style.cornerRadius
          color: Style.normalFillFor(root.foreground, Color.accent)

          Row {
            anchors.fill: parent
            anchors.margins: Style.space(8)
            spacing: Style.space(9)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: "󰅖"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Column {
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - Style.space(28)

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: "No recent sessions"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: "Start a Hermes session to see it here"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        Repeater {
          model: root.status.recentSessions || []
          BorderSurface {
            id: recentSessionCard
            required property var modelData
            width: parent.width
            height: Style.space(56)
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.foreground, Color.accent)

            Image {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(24)
              height: Style.space(24)
              source: modelData.avatarUrl
                ? String(modelData.avatarUrl)
                : Qt.resolvedUrl("assets/hermes-watcher.svg")
              sourceSize.width: width * Screen.devicePixelRatio
              sourceSize.height: height * Screen.devicePixelRatio
              fillMode: Image.PreserveAspectFit
              asynchronous: true
            }

            Column {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(40)
              anchors.right: resumeGlyph.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(1)

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: String(modelData.description || "Untitled Hermes session")
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: String(modelData.profile || "Hermes Agent") + " · "
                  + Model.formatRelative(modelData.recentAt, root.status.generatedAt)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            Text {
              id: resumeGlyph
              anchors.right: parent.right
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: "󰑐"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                if (root.monitor
                    && root.monitor.resumeSession(String(modelData.profile || ""), String(modelData.sessionId || "")))
                  root.close()
              }
            }
          }
        }
      }
      }
    }
  }
}
}
