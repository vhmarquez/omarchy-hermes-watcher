import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OmarchyPluginContractTests(unittest.TestCase):
    def test_inotify_loss_forces_a_full_health_reconciliation(self):
        collector = (ROOT / "hermes_bot_status.py").read_text()

        self.assertIn("if not intact:\n                    watch_complete = False\n                    health_due = True", collector)

    def test_readme_documents_milestone_three_and_four_contracts(self):
        readme = (ROOT / "README.md").read_text()

        self.assertIn("persistent event-driven collector", readme)
        self.assertIn("30-second health scan", readme)
        self.assertIn("0.25% of one CPU core", readme)
        self.assertIn("showWorkDescription", readme)
        self.assertIn("showRecentSessionTitles", readme)
        self.assertIn("Clear Watcher history", readme)
        self.assertIn("256 KiB", readme)

    def test_disabled_description_setting_reports_old_observers_that_need_restart(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn('property string privacyError: ""', service)
        self.assertIn('workDescriptionPolicyLoaded', service)
        self.assertIn('Restart Hermes to enforce hidden work descriptions', service)

    def test_history_has_time_retention_and_an_explicit_confirmed_clear_action(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertEqual(
            manifest["barWidget"]["defaults"].get("historyRetentionDays"),
            30,
        )
        self.assertIn('function clearHistory()', service)
        self.assertIn('"clear-history"', service)
        self.assertIn('"--max-age-sec"', service)
        self.assertIn('text: root.clearHistoryArmed ? "Confirm clear history"', widget)
        self.assertIn('root.monitor.clearHistory()', widget)

    def test_history_clear_does_not_race_notification_delivery(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn("if (notifyProcess.running || ackProcess.running)", service)
        self.assertIn("notificationQueue = []", service)
        self.assertIn("acknowledgementQueue = []", service)
        self.assertIn("if (clearHistoryInProgress) return", service)

    def test_product_contract_records_milestone_zero_decisions(self):
        contract = (ROOT / "docs/product-contract.md").read_text()

        self.assertIn("Profile", contract)
        self.assertIn("Session", contract)
        self.assertIn("Turn", contract)
        self.assertIn("Agent", contract)
        self.assertIn("Profile filtering applies to every Watcher-visible profile surface", contract)
        self.assertIn("Task descriptions remain enabled by default for the 0.x series", contract)
        self.assertIn("Recent means resumable sessions", contract)
        self.assertIn("Completion outcomes belong in Activity", contract)
        self.assertIn("Hermes Agent 0.21 or newer", contract)
        self.assertIn("Omarchy 4.0 or newer", contract)
        self.assertIn("Python 3.11 or newer", contract)
        self.assertIn("Idle performance target", contract)
        self.assertIn("Milestone 3 is implemented", contract)
        self.assertIn("Milestone 4 privacy controls", contract)
        self.assertIn("Milestone 6 future work", contract)

    def test_readme_documents_milestone_two_recovery_contracts(self):
        readme = (ROOT / "README.md").read_text()

        self.assertIn("persistent delivery claim", readme)
        self.assertIn("five minutes", readme)
        self.assertIn("failed profile IDs", readme)
        self.assertIn("executable Quickshell service smoke test", readme)

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
        self.assertEqual(defaults.get("healthScanIntervalSec"), 30)
        self.assertTrue(defaults["notifyOnSuccess"])
        self.assertTrue(defaults["notifyOnFailure"])
        self.assertIs(defaults.get("showWorkDescription"), True)
        self.assertIs(defaults.get("showRecentSessionTitles"), True)
        self.assertEqual(defaults["profileFilter"], "")

    def test_qml_uses_shared_service_and_safe_process_argv(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()
        icon = ROOT / "assets" / "hermes-watcher.svg"
        self.assertIn('var command = ["python3", "-B", root.collectorPath, "watch"', service)
        self.assertIn('["timeout", "15s", "python3", root.collectorPath, "deliver-notification"', service)
        self.assertIn('Qt.resolvedUrl("assets/hermes-watcher.svg")', service)
        self.assertIn('"--icon", root.notificationIconPath', service)
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
        self.assertIn("id: agentTabs", widget)
        self.assertIn("fontFamily: root.fontFamily", widget)
        self.assertNotIn("readonly property color barForeground", widget)
        self.assertIn("root.foreground", widget)
        self.assertIn("radius: Style.cornerRadius", widget)
        self.assertNotIn("font.pixelSize: 8", widget)
        self.assertNotIn("radius: 7", widget)

    def test_active_and_recent_tabs_are_under_the_sessions_section(self):
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn('property string agentTab: "active"', widget)
        self.assertIn("id: agentTabs", widget)
        self.assertIn('model: [{ value: "active", label: "Active" },', widget)
        self.assertIn('{ value: "recent", label: "Recent" }]', widget)
        self.assertIn("anchors.left: parent.left", widget)
        self.assertIn("id: sessionTabUnderline", widget)
        self.assertIn('visible: root.agentTab === String(modelData.value)', widget)
        self.assertIn("color: Color.accent", widget)
        self.assertIn("onClicked: root.selectAgentTab(String(modelData.value))", widget)
        self.assertNotIn("ButtonGroup {\n          id: agentTabs", widget)
        self.assertIn("function selectAgentTab(value)", widget)
        self.assertIn("if (dx < 0) root.selectAgentTab(\"active\")", widget)
        self.assertIn("else if (dx > 0) root.selectAgentTab(\"recent\")", widget)
        self.assertEqual(widget.count('text: "SESSIONS"'), 1)

        profiles_start = widget.index("id: profileLauncherFlick")
        sessions_start = widget.index('text: "SESSIONS"')
        tabs_start = widget.index("id: agentTabs")
        active_start = widget.index("id: activeTabContent")
        recent_start = widget.index("id: recentTabContent")
        self.assertLess(profiles_start, sessions_start)
        self.assertLess(sessions_start, tabs_start)
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
        self.assertIn("validatedArray(value.recentSessions, 100, validRecentSession).slice(0, 6)", (ROOT / "Model.js").read_text())
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
            '["hermes", "--profile", profile, "--resume", sessionId]',
            service,
        )
        self.assertIn('"HERMES_HOME=" + hermesRoot', service)
        self.assertIn('var hermesRoot = String(root.status.hermesRoot || "")', service)
        self.assertIn("model: root.status.recentSessions || []", recent)
        self.assertIn('text: String(modelData.description || "Untitled Hermes session")', recent)
        self.assertIn("cursorShape: Qt.PointingHandCursor", recent)
        self.assertIn(
            'root.monitor.resumeSession(String(modelData.profile || ""), String(modelData.sessionId || ""))',
            recent,
        )
        self.assertNotIn("root.close()", recent)
        self.assertIn("function onLaunchSucceeded() { root.close() }", widget)

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
        self.assertIn('visible: root.agentTab === "active" && root.monitor', widget)
        self.assertIn('&& root.monitor.hasSnapshot', widget)
        self.assertIn('&& root.onlineBotCount === 0', widget)

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

    def test_uninstrumented_online_session_is_not_labeled_idle(self):
        widget = (ROOT / "BarWidget.qml").read_text()
        cards = widget.split("id: botCard", 1)[1].split("BorderSurface {", 1)[0]

        self.assertIn("modelData.observerLoaded", cards)
        self.assertIn('"Activity unavailable — restart Hermes"', cards)
        self.assertIn('"Idle — awaiting a task"', cards)

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
            '["hermes", "--profile", profile]',
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
        self.assertIn(
            'text: botCard.isWorking ? "󰔟" : (botCard.observerLoaded ? "󰖟" : "󰅙")',
            cards,
        )
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
        self.assertIn("var queue = acknowledgementQueue.slice()", service)
        self.assertNotIn("slice(-99)", service)
        self.assertIn("seenFailureEventId", widget)

    def test_notification_backpressure_does_not_mark_undelivered_events_attempted(self):
        service = (ROOT / "Service.qml").read_text()
        apply_snapshot = service.split("function applySnapshot(raw)", 1)[1].split(
            "function finishSnapshot", 1
        )[0]

        self.assertIn("readonly property int maxNotificationQueueDepth: 100", service)
        capacity_check = apply_snapshot.index(
            "if (queue.length >= maxNotificationQueueDepth) break"
        )
        attempted_write = apply_snapshot.index("attemptedNotifications[event.eventId] = true")
        self.assertLess(capacity_check, attempted_write)
        self.assertIn(
            "if (acknowledgementQueue.length >= maxNotificationQueueDepth) return",
            service,
        )

    def test_persistent_claim_contention_is_not_reported_as_delivery_failure(self):
        service = (ROOT / "Service.qml").read_text()
        finish = service.split("function finishNotificationResult(exitCode)", 1)[1].split(
            "function finishNotification", 1
        )[0]
        delivered = service.split("function finishNotification(delivered)", 1)[1].split(
            "function startAcknowledgement", 1
        )[0]

        self.assertIn("if (exitCode === 75)", finish)
        self.assertIn('notificationError = ""', finish)
        self.assertNotIn("enqueueAcknowledgement(eventId)", delivered)
        self.assertIn("onExited: function(exitCode) { root.finishNotificationResult(exitCode) }", service)

    def test_stream_output_is_validated_before_application_and_unexpected_exit_retries(self):
        service = (ROOT / "Service.qml").read_text()
        self.assertIn("id: collectorProcess", service)
        self.assertIn("stdout: SplitParser", service)
        self.assertIn("onRead: function(line) { root.applySnapshot(line) }", service)
        self.assertIn("property bool collectorWanted: true", service)
        self.assertIn("id: collectorWatchdogTimer", service)
        self.assertIn("repeat: false", service)
        self.assertIn("if (!root.collectorWanted) return", service)
        self.assertIn("root.collectorWanted = false", service)
        self.assertIn(
            'recordCollectorFailure("Hermes Watcher collector exited unexpectedly")',
            service,
        )

    def test_observer_setup_failure_has_bounded_retry_and_manual_recovery(self):
        service = (ROOT / "Service.qml").read_text()
        self.assertIn("id: setupRetryTimer", service)
        self.assertIn("interval: 30000", service)
        self.assertIn("if (!root.setupReady && !setupProcess.running)", service)
        refresh = service.split("function refresh(force)", 1)[1].split("function launchHermes", 1)[0]
        self.assertIn("root.startSetup()", refresh)

    def test_service_reconciles_profiles_discovered_after_initial_setup(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn('property string reconciledProfilesKey: ""', service)
        self.assertIn("property bool setupReconciliation: false", service)
        self.assertIn("function profileSetKey(profiles)", service)
        self.assertIn("function reconcileProfiles(profiles)", service)
        self.assertIn("root.startSetup(true)", service)
        self.assertIn("reconcileProfiles(parsed.availableProfiles || [])", service)
        self.assertIn("root.startSetup(root.setupReady)", service)

    def test_invalid_snapshot_preserves_the_last_real_status(self):
        service = (ROOT / "Service.qml").read_text()
        apply_snapshot = service.split("function applySnapshot(raw)", 1)[1].split(
            "function recordCollectorFailure", 1
        )[0]
        invalid_guard = apply_snapshot.find("if (!parsed.ok)")
        status_assignment = apply_snapshot.index("status = parsed")
        self.assertGreaterEqual(invalid_guard, 0)
        self.assertLess(invalid_guard, status_assignment)
        self.assertIn(
            'recordCollectorFailure(parsed.lastError || "Hermes Watcher status unavailable")',
            apply_snapshot,
        )

    def test_service_tracks_loading_freshness_and_subsystem_errors_separately(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn("property bool hasSnapshot: false", service)
        self.assertIn("property real lastSuccessfulSnapshotAt: 0", service)
        self.assertIn('property string setupError: ""', service)
        self.assertIn('property string statusError: ""', service)
        self.assertIn('property string notificationError: ""', service)
        self.assertIn('property string acknowledgementError: ""', service)
        self.assertIn('property string consumerError: ""', service)
        self.assertIn('property string actionError: ""', service)
        self.assertIn("readonly property bool refreshing: collectorRefreshPending", service)
        self.assertIn("hasSnapshot = true", service)
        self.assertIn("lastSuccessfulSnapshotAt = Number(parsed.generatedAt || Date.now() / 1000)", service)
        self.assertIn('consumerError = String(parsed.notificationError || "")', service)

    def test_service_bounds_every_child_process_with_a_timeout(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn('["timeout", "30s", root.setupScriptPath]', service)
        self.assertIn('var command = ["python3", "-B", root.collectorPath, "watch"', service)
        self.assertIn("collectorProcess.signal(15)", service)
        self.assertIn('["timeout", "15s", "python3", root.collectorPath, "deliver-notification"', service)
        self.assertIn('["timeout", "5s", "python3", root.collectorPath, "acknowledge"', service)
        self.assertIn('["timeout", "30s", "python3", "-B", root.collectorPath,', service)
        self.assertIn('"prune", "--keep-terminal", "100", "--max-age-sec"', service)

    def test_setup_and_collector_failures_use_bounded_exponential_backoff(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn("property int setupFailureCount: 0", service)
        self.assertIn("property int collectorFailureCount: 0", service)
        self.assertIn("property real collectorRetryAfter: 0", service)
        self.assertIn("Math.min(300000, 1000 * Math.pow(2", service)
        self.assertIn("Math.min(60000, 1000 * Math.pow(2", service)
        self.assertIn("function refresh(force)", service)
        self.assertIn("if (force !== true && Date.now() < collectorRetryAfter) return", service)

    def test_user_refresh_bypasses_automatic_collector_backoff(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn("function refresh(): string { root.refresh(true); return \"ok\" }", service)
        self.assertGreaterEqual(widget.count("root.monitor.refresh(true)"), 2)
        self.assertIn("if (force === true) {", service)
        self.assertIn("collectorRetryAfter = 0", service)
        self.assertIn("collectorRetryTimer.stop()", service)

    def test_partial_profile_setup_keeps_status_available_and_retries(self):
        service = (ROOT / "Service.qml").read_text()

        self.assertIn("id: setupStderr", service)
        self.assertIn('setupStderr.text.indexOf("completed with profile warnings")', service)
        self.assertIn("property var failedSetupProfiles: []", service)
        self.assertIn("function parseFailedSetupProfiles(stderrText)", service)
        self.assertIn('"Could not instrument Hermes profiles: " + failedSetupProfiles.join(", ")', service)
        self.assertIn("root.setupReady = true", service)
        self.assertIn("setupRetryTimer.restart()", service)

    def test_launch_and_resume_wait_for_a_bounded_process_result(self):
        service = (ROOT / "Service.qml").read_text()
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn("property bool actionInProgress: false", service)
        self.assertIn("signal launchSucceeded()", service)
        self.assertIn("id: launchProcess", service)
        self.assertIn(
            '["omarchy", "launch", "terminal", "env", "HERMES_HOME=" + hermesRoot]',
            service,
        )
        self.assertIn('launchProcess.command = ["timeout", "10s"].concat(terminalCommand)', service)
        self.assertIn('actionError = "Another Hermes launch is already in progress"', service)
        self.assertIn('actionError = "Could not open a Hermes terminal"', service)
        self.assertIn("function onLaunchSucceeded() { root.close() }", widget)

    def test_panel_distinguishes_initial_loading_from_a_healthy_empty_state(self):
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn('return "Preparing Hermes Watcher"', widget)
        self.assertIn('return "Loading sessions"', widget)
        self.assertIn('text: "Preparing Hermes Watcher…"', widget)
        self.assertIn("visible: root.monitor && !root.monitor.hasSnapshot", widget)
        self.assertIn("visible: root.agentTab === \"active\" && root.monitor", widget)
        self.assertIn("root.monitor.hasSnapshot && root.onlineBotCount === 0", widget)

    def test_stale_snapshot_age_uses_the_service_clock(self):
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn("root.monitor.statusClockSec", widget)
        self.assertNotIn(
            "Model.formatRelative(root.monitor.lastSuccessfulSnapshotAt, Date.now() / 1000)",
            widget,
        )

    def test_panel_explains_when_no_launchable_profiles_are_available(self):
        widget = (ROOT / "BarWidget.qml").read_text()

        self.assertIn('text: "No launchable Hermes profiles"', widget)
        self.assertIn('text: "Check Hermes installation and profile setup"', widget)
        self.assertIn("root.status.availableProfiles || []", widget)

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
