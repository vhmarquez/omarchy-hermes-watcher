import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OmarchyPluginContractTests(unittest.TestCase):
    def test_manifest_exposes_persistent_service_and_bar_widget(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "vhm.hermes-bots")
        self.assertEqual(manifest["name"], "Hermes Watcher")
        self.assertEqual(manifest["barWidget"]["displayName"], "Hermes Watcher")
        self.assertEqual(set(manifest["kinds"]), {"service", "bar-widget"})
        self.assertTrue(manifest["keepLoaded"])
        self.assertEqual(manifest["entryPoints"]["service"], "Service.qml")
        self.assertEqual(manifest["entryPoints"]["barWidget"], "BarWidget.qml")
        defaults = manifest["barWidget"]["defaults"]
        self.assertEqual(defaults["pollIntervalSec"], 2)
        self.assertTrue(defaults["notifyOnSuccess"])
        self.assertTrue(defaults["notifyOnFailure"])
        self.assertEqual(defaults["profileFilter"], "")

    def test_qml_uses_shared_service_and_safe_process_argv(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        icon = ROOT / "assets" / "hermes-watcher.svg"
        self.assertIn('command: ["python3", root.collectorPath', service)
        self.assertIn('["notify-send", "--app-name=Hermes Watcher"', service)
        self.assertIn('Qt.resolvedUrl("assets/hermes-watcher.svg")', service)
        self.assertIn('"--icon=" + root.notificationIconPath', service)
        self.assertNotIn("--icon=applications-development", service)
        self.assertTrue(icon.is_file())
        self.assertIn("<svg", icon.read_text())
        self.assertNotIn('bash', service)
        self.assertIn('serviceFor("vhm.hermes-bots")', widget)
        self.assertIn("Text.PlainText", widget)

    def test_panel_header_matches_bluetooth_layout_and_adds_status_subtitle(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        header = widget.split("// ---------- Hero: Hermes Watcher", 1)[1].split("PanelSeparator {", 1)[0]
        self.assertIn("implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, notificationSwitch.implicitHeight)", header)
        self.assertIn("id: bluetoothIconReference", header)
        self.assertIn('text: "󰂯"', header)
        self.assertIn("visible: false", header)
        self.assertIn("id: heroIcon", header)
        self.assertIn("implicitWidth: bluetoothIconReference.implicitWidth", header)
        self.assertIn("implicitHeight: bluetoothIconReference.implicitHeight", header)
        self.assertIn('text: "\\u2695\\uFE0E"', header)
        self.assertIn('font.family: "Noto Sans Symbols"', header)
        self.assertIn("color: root.foreground", header)
        self.assertIn("font.pixelSize: Math.round(Style.font.display * 1.55)", header)
        self.assertIn("anchors.verticalCenterOffset: -Style.space(5)", header)
        self.assertIn("anchors.verticalCenter: parent.verticalCenter", header)
        self.assertIn("id: heroLabels", header)
        self.assertIn("spacing: Style.space(2)", header)
        self.assertIn('text: "Hermes Watcher"', header)
        self.assertIn("text: root.heroStatusText.toUpperCase()", header)
        self.assertIn("font.pixelSize: Style.font.caption", header)
        self.assertIn("font.letterSpacing: 1.2", header)
        self.assertIn("id: notificationSwitch", header)
        self.assertIn("checked: root.notificationsEnabled", header)
        self.assertIn("onToggled: root.toggleNotifications()", header)
        self.assertNotIn("PanelHero {", header)
        self.assertNotIn("PanelActionButton {", header)

    def test_panel_uses_native_omarchy_theme_components_and_tokens(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn("id: heroLabels", widget)
        self.assertIn("ToggleSwitch {", widget)
        self.assertGreaterEqual(widget.count("PanelSeparator {"), 1)
        self.assertIn("ButtonGroup {", widget)
        self.assertIn("fontFamily: root.fontFamily", widget)
        self.assertNotIn("readonly property color barForeground", widget)
        self.assertIn("root.foreground", widget)
        self.assertIn("radius: Style.cornerRadius", widget)
        self.assertNotIn("font.pixelSize: 8", widget)
        self.assertNotIn("radius: 7", widget)

    def test_active_and_recent_sessions_are_tabs_below_agent_profiles(self):
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn('property string agentTab: "active"', widget)
        self.assertIn("id: agentTabs", widget)
        self.assertIn('options: [{ value: "active", label: "Active" },', widget)
        self.assertIn('{ value: "recent", label: "Recent" }]', widget)
        self.assertIn("value: root.agentTab", widget)
        self.assertIn("onChanged: function(value) { root.selectAgentTab(value) }", widget)
        self.assertIn("function selectAgentTab(value)", widget)
        self.assertIn("if (dx < 0) root.selectAgentTab(\"active\")", widget)
        self.assertIn("else if (dx > 0) root.selectAgentTab(\"recent\")", widget)

        profiles_start = widget.index("id: profileLauncherFlick")
        tabs_start = widget.index("id: agentTabs")
        active_start = widget.index("id: activeTabContent")
        recent_start = widget.index("id: recentTabContent")
        self.assertLess(profiles_start, tabs_start)
        self.assertLess(tabs_start, active_start)
        self.assertLess(active_start, recent_start)

        active = widget[active_start:recent_start]
        recent = widget[recent_start:]
        self.assertIn('visible: root.agentTab === "active"', active)
        self.assertIn("model: root.status.onlineProfiles || []", active)
        self.assertIn('visible: root.agentTab === "recent"', recent)
        self.assertIn("model: root.status.recentSessions || []", recent)
        self.assertIn('text: "No recent sessions"', recent)
        self.assertEqual(widget.count('text: "AGENTS"'), 1)
        self.assertNotIn('PanelSectionHeader {\n        text: "RECENT"', widget)

    def test_recent_section_is_bounded_to_six_sessions_without_outcome_filters(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        recent = widget.split("id: recentTabContent", 1)[1]
        history_schema = next(
            item for item in manifest["barWidget"]["schema"] if item["key"] == "historyLimit"
        )

        self.assertEqual(manifest["barWidget"]["defaults"]["historyLimit"], 6)
        self.assertEqual(history_schema["max"], 6)
        self.assertIn('Math.min(6, Math.max(1, Number(setting("historyLimit", 6))))', service)
        self.assertIn("value.recentSessions.slice(0, 6)", (ROOT / "Model.js").read_text())
        self.assertNotIn("recentFilter", widget)
        self.assertNotIn("function filteredRecent()", widget)
        self.assertNotIn("ButtonGroup {", recent)
        self.assertNotIn('text: "No matching outcomes"', recent)

    def test_recent_rows_show_session_descriptions_and_resume_the_exact_session(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        recent = widget.split("id: recentTabContent", 1)[1]

        self.assertIn("readonly property var recentSessions", service)
        self.assertIn("function resumeSession(profile, sessionId)", service)
        self.assertIn('/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/', service)
        self.assertIn(
            '["omarchy", "launch", "terminal", "hermes", "--profile", profile, "--resume", sessionId]',
            service,
        )
        self.assertIn("environment: ({ HERMES_HOME: hermesRoot })", service)
        self.assertIn('var hermesRoot = String(root.status.hermesRoot || "")', service)
        self.assertIn("model: root.status.recentSessions || []", recent)
        self.assertIn('text: String(modelData.description || "Untitled Hermes session")', recent)
        self.assertIn("cursorShape: Qt.PointingHandCursor", recent)
        self.assertIn(
            'root.monitor.resumeSession(String(modelData.profile || ""), String(modelData.sessionId || ""))',
            recent,
        )
        self.assertIn("root.close()", recent)

    def test_recent_session_rows_use_descriptions_without_outcome_state_labels(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        recent = widget.split("id: recentTabContent", 1)[1]

        self.assertNotIn("Model.stateGlyph(modelData.state)", recent)
        self.assertNotIn("Model.stateLabel(modelData.state)", widget)
        self.assertNotIn('modelData.activeTurnCount > 0 ? "Working" : "Online"', widget)
        self.assertIn('text: String(modelData.description || "Untitled Hermes session")', recent)
        self.assertIn('String(modelData.profile || "Hermes Agent")', recent)
        self.assertIn('String(modelData.model || "Unknown model")', widget)
        self.assertIn('String(modelData.platform || "local")', widget)

    def test_panel_shows_an_explicit_idle_state(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn('text: "No Agents running"', widget)
        self.assertIn('visible: root.agentTab === "active" && root.onlineBotCount === 0', widget)

    def test_panel_shows_one_card_per_open_session_even_when_no_turn_hook_is_active(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn("readonly property int onlineBotCount", service)
        self.assertIn("readonly property var onlineProfiles", service)
        self.assertIn("model: root.status.onlineProfiles || []", widget)
        self.assertNotIn("modelData.sessionCount", widget)
        self.assertNotIn("open session", widget.lower())
        self.assertIn('text: "AGENTS"', widget)
        self.assertIn("visible: root.onlineBotCount > 0", widget)
        self.assertIn('String(root.onlineBotCount)', widget)
        self.assertIn("anchors.rightMargin: -Style.spacing.sm", widget)
        self.assertIn("anchors.topMargin: -Style.spacing.xxs", widget)
        self.assertIn('root.onlineBotCount > 9 ? "9+"', widget)
        self.assertNotIn("width: Math.max(Style.spacing", widget)
        self.assertIn("focusTarget: keyCatcher", widget)
        self.assertIn("PanelKeyCatcher {", widget)
        self.assertIn("ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }", widget)
        self.assertNotIn("id: filterFlick", widget)

    def test_available_profile_icons_launch_new_profile_sessions_with_safe_argv(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        agents_start = widget.index('text: "AGENTS"')
        profiles_start = widget.index("model: root.status.availableProfiles || []")
        tabs_start = widget.index("id: agentTabs")
        active_agents_start = widget.index("model: root.status.onlineProfiles || []")
        recent_start = widget.index("id: recentTabContent")

        self.assertIn("readonly property var availableProfiles", service)
        self.assertIn("function launchProfile(profile)", service)
        self.assertIn('/^[a-z0-9][a-z0-9_-]{0,63}$/', service)
        self.assertIn('["hermes", "test", "tmp", "root", "sudo"]', service)
        self.assertIn(
            '["omarchy", "launch", "terminal", "hermes", "--profile", profile]',
            service,
        )
        self.assertNotIn('"bash", "-c"', service)
        self.assertNotIn('text: "START A SESSION"', widget)
        self.assertEqual(widget.count('text: "AGENTS"'), 1)
        self.assertLess(agents_start, profiles_start)
        self.assertLess(profiles_start, tabs_start)
        self.assertLess(tabs_start, active_agents_start)
        self.assertLess(active_agents_start, recent_start)
        self.assertIn("model: root.status.availableProfiles || []", widget)
        self.assertIn("modelData.avatarUrl", widget)
        self.assertIn("fillMode: Image.PreserveAspectFit", widget)
        self.assertIn("root.monitor.launchProfile(String(modelData.profile || \"\"))", widget)

    def test_profile_launchers_render_as_icon_only_with_name_on_hover(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        launchers = widget.split("id: profileLauncherFlick", 1)[1].split(
            "model: root.status.onlineProfiles || []", 1
        )[0]

        self.assertIn("Item {\n              id: profileLauncher", launchers)
        self.assertNotIn("BorderSurface {", launchers)
        self.assertNotIn("\n              Text {", launchers)
        self.assertIn("anchors.centerIn: parent", launchers)
        self.assertIn("visible: profileLauncherMouse.containsMouse", launchers)
        self.assertIn('text: String(modelData.profile || "Hermes")', launchers)

    def test_active_agent_cards_show_the_current_work_description(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn("modelData.workDescription", widget)
        self.assertIn('"Current task unavailable"', widget)
        self.assertIn("Text.PlainText", widget)

    def test_active_agent_cards_expand_the_full_available_description_when_clicked(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        cards = widget.split("id: botCard", 1)[1].split("BorderSurface {", 1)[0]
        self.assertIn("property var expandedSessionKeys: ({})", widget)
        self.assertIn("function sessionDescriptionExpanded(sessionKey)", widget)
        self.assertIn("function toggleSessionDescription(sessionKey)", widget)
        self.assertIn("function pruneExpandedSessionKeys()", widget)
        self.assertIn("var snapshot = root.status || ({})", widget)
        self.assertIn("root.pruneExpandedSessionKeys()", widget)
        self.assertIn('readonly property string sessionKey: String(modelData.sessionKey || "")', cards)
        self.assertIn(
            "readonly property bool descriptionExpanded: root.sessionDescriptionExpanded(sessionKey)",
            cards,
        )
        self.assertNotIn("property bool descriptionExpanded: false", cards)
        self.assertIn("TapHandler {", cards)
        self.assertIn("onTapped: root.toggleSessionDescription(botCard.sessionKey)", cards)
        self.assertIn("wrapMode: botCard.descriptionExpanded ? Text.Wrap : Text.NoWrap", cards)
        self.assertIn("elide: botCard.descriptionExpanded ? Text.ElideNone : Text.ElideRight", cards)
        context_hover = cards.split("id: contextHover", 1)[1].split("}", 1)[0]
        self.assertIn("acceptedButtons: Qt.NoButton", context_hover)

    def test_agent_cards_use_equal_outer_padding_and_content_driven_height(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        cards = widget.split("id: botCard", 1)[1].split("BorderSurface {", 1)[0]
        self.assertIn("readonly property real cardPadding: Style.space(8)", cards)
        self.assertIn("id: cardContent", cards)
        self.assertIn(
            "implicitHeight: Math.max(cardContent.implicitHeight, agentIcon.height, agentState.implicitHeight) + 2 * cardPadding",
            cards,
        )
        self.assertIn("height: implicitHeight", cards)
        self.assertIn("anchors.topMargin: botCard.cardPadding", cards)
        self.assertNotIn("height: Style.space(72)", cards)

    def test_agent_card_places_unbolded_session_time_beside_the_title(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        cards = widget.split("id: botCard", 1)[1].split("BorderSurface {", 1)[0]
        self.assertIn("id: agentHeader", cards)
        self.assertIn("id: agentTitle", cards)
        self.assertIn("id: sessionTime", cards)
        self.assertLess(cards.index("id: agentTitle"), cards.index("id: sessionTime"))
        self.assertIn('text: "(" + Model.formatDuration(modelData.runningForSec) + ")"', cards)
        session_time = cards.split("id: sessionTime", 1)[1].split("}", 1)[0]
        self.assertIn("font.bold: false", session_time)

    def test_agent_cards_use_native_profile_avatars_with_a_top_right_state_badge(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        cards = widget.split("id: botCard", 1)[1].split("BorderSurface {", 1)[0]
        self.assertIn("id: agentIcon", cards)
        self.assertIn("modelData.avatarUrl", cards)
        self.assertIn("String(modelData.avatarUrl)", cards)
        self.assertIn('Qt.resolvedUrl("assets/hermes-watcher.svg")', cards)
        self.assertIn("fillMode: Image.PreserveAspectFit", cards)
        self.assertIn("id: agentState", cards)
        self.assertIn('text: modelData.activeTurnCount > 0 ? "󰔟" : "󰖟"', cards)
        self.assertIn("anchors.top: parent.top", cards)
        self.assertIn("anchors.right: parent.right", cards)
        self.assertLess(cards.index("id: agentIcon"), cards.index("id: agentState"))

    def test_active_and_idle_bot_cards_show_context_stats_only_on_bar_hover(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        context_guard = widget.split("readonly property bool hasContext:", 1)[1].split(
            "readonly property real contextFraction:", 1
        )[0]
        self.assertNotIn('text: modelData.contextIsLastKnown ? "LAST CONTEXT" : "CONTEXT"', widget)
        self.assertNotIn('text: Math.round(Number(modelData.contextPercent || 0)) + "%"', widget)
        self.assertNotIn("anchors.top: contextTrack.bottom", widget)
        self.assertIn("id: contextHover", widget)
        self.assertIn("hoverEnabled: true", widget)
        self.assertIn("visible: contextHover.containsMouse", widget)
        self.assertIn('(modelData.contextIsLastKnown ? "Last context: " : "Context: ")', widget)
        self.assertIn('"Model: " + String(modelData.model || "Unavailable")', widget)
        self.assertIn('"Reasoning: " + String(modelData.reasoningLevel || "Unavailable")', widget)
        self.assertIn("modelData.contextPercent", widget)
        self.assertIn("modelData.contextUsed", widget)
        self.assertIn("modelData.contextMax", widget)
        self.assertIn("Model.formatTokenCount", widget)
        self.assertNotIn("modelData.activeTurnCount", context_guard)
        self.assertIn("Number(modelData.contextMax || 0) > 0", context_guard)
        self.assertIn("Math.min(1,", widget)

    def test_bar_icon_uses_the_official_theme_colored_hermes_favicon(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        bar_icon = widget.split("iconComponent: Component {", 1)[1].split("onPressed:", 1)[0]
        favicon = ROOT / "assets" / "hermes-agent-favicon.svg"
        self.assertTrue(favicon.is_file())
        self.assertEqual(
            favicon.read_text(),
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
            '  <text y=".9em" font-size="90">⚕</text>\n'
            '</svg>\n',
        )
        self.assertIn("id: officialFavicon", bar_icon)
        self.assertIn('text: "\\u2695\\uFE0E"', bar_icon)
        self.assertIn("color: root.foreground", bar_icon)
        self.assertNotIn("color: root.barForeground", bar_icon)
        count = bar_icon.split("id: countText", 1)[1]
        favicon = bar_icon.split("id: officialFavicon", 1)[1].split("id: countText", 1)[0]
        self.assertIn("color: root.foreground", count)
        self.assertIn('font.family: "Noto Sans Symbols"', favicon)
        self.assertIn("font.family: root.fontFamily", count)
        self.assertNotIn('source: Qt.resolvedUrl("assets/hermes-agent-favicon.svg")', bar_icon)
        self.assertNotIn('source: Qt.resolvedUrl("assets/hermes-watcher.svg")', bar_icon)
        self.assertNotIn("OpticalGlyph {", bar_icon)

    def test_widget_uses_the_official_hermes_favicon_in_the_panel_header(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertGreaterEqual(widget.count('text: "\\u2695\\uFE0E"'), 2)
        self.assertNotIn("☤", widget)
        self.assertNotIn("󱚣", widget)

    def test_widget_imports_quickshell_io_for_ipc_handler(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn("import Quickshell.Io", widget)

    def test_widget_can_persist_and_toggle_notifications(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn('"--profile-filter"', service)
        self.assertIn("function toggleNotifications()", widget)
        self.assertIn("updateEntryInline", widget)
        self.assertIn("Qt.MiddleButton", widget)
        self.assertIn('"prune", "--keep-terminal"', service)
        self.assertIn("Flickable {", widget)
        self.assertIn("contentHeight: content.implicitHeight", widget)
        self.assertIn("function notificationsOn():", service)
        self.assertIn("function notificationsOff():", service)
        self.assertIn("function notificationsToggle():", service)

    def test_notification_failures_retry_and_viewed_failures_stay_seen(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        self.assertIn("property var notificationRetryAfter", service)
        self.assertIn("id: notificationRetryTimer", service)
        self.assertIn("id: ackRetryTimer", service)
        self.assertIn("function(exitCode)", service)
        self.assertIn('setting("notificationsEnabled", true)', service)
        self.assertIn("attempts >= 3", service)
        self.assertIn("property var acknowledgementQueue", service)
        self.assertIn("function compactNotificationState", service)
        self.assertIn("slice(-99)", service)
        self.assertIn("seenFailureEventId", widget)

    def test_snapshot_output_is_applied_only_after_a_successful_process_exit(self):
        service = (ROOT / "Service.qml").read_text()
        self.assertIn("function finishSnapshot(exitCode, exitStatus, raw)", service)
        self.assertIn("if (exitCode !== 0 || exitStatus !== 0)", service)
        self.assertIn("id: snapshotStdout", service)
        self.assertIn(
            "onExited: function(exitCode, exitStatus) { root.finishSnapshot(exitCode, exitStatus, snapshotStdout.text) }",
            service,
        )
        self.assertNotIn("onStreamFinished: root.applySnapshot(text)", service)

    def test_observer_setup_failure_has_bounded_retry_and_manual_recovery(self):
        service = (ROOT / "Service.qml").read_text()
        self.assertIn("id: setupRetryTimer", service)
        self.assertIn("interval: 30000", service)
        self.assertIn("if (!root.setupReady && !setupProcess.running)", service)
        refresh = service.split("function refresh()", 1)[1].split("function addEventIds", 1)[0]
        self.assertIn("root.startSetup()", refresh)

    def test_invalid_snapshot_preserves_the_last_real_status(self):
        service = (ROOT / "Service.qml").read_text()
        apply_snapshot = service.split("function applySnapshot(raw)", 1)[1].split(
            "function finishSnapshot", 1
        )[0]
        invalid_guard = apply_snapshot.find("if (!parsed.ok)")
        status_assignment = apply_snapshot.index("status = parsed")
        self.assertGreaterEqual(invalid_guard, 0)
        self.assertLess(invalid_guard, status_assignment)
        self.assertIn('lastError = parsed.lastError || "Hermes Watcher status unavailable"', apply_snapshot)

    def test_service_manages_observer_lifecycle(self):
        service = (ROOT / "Service.qml").read_text()
        self.assertIn("id: setupProcess", service)
        self.assertIn("property bool setupReady", service)
        self.assertIn("running: root.setupReady", service)
        self.assertIn("Component.onDestruction", service)
        self.assertIn("Quickshell.execDetached", service)
        self.assertIn("monitor.setNotificationsEnabled", (ROOT / "BarWidget.qml").read_text())

    def test_lifecycle_scripts_serialize_setup_and_removal(self):
        setup = (ROOT / "scripts/setup-profiles").read_text()
        remove = (ROOT / "scripts/remove-profiles").read_text()
        for script in (setup, remove):
            self.assertIn('lock_path="${XDG_RUNTIME_DIR:-/tmp}/vhm-hermes-bots-${UID}.lock"', script)
            self.assertIn("flock 9", script)
            self.assertIn("Managed lock changed while opening", script)
            self.assertIn("chmod 0600", script)
        self.assertIn("--if-disabled", remove)
        self.assertIn("--if-disabled", (ROOT / "scripts/cleanup-observer").read_text())


if __name__ == "__main__":
    unittest.main()
