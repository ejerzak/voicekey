import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services

// Clicking the agent (Hermes) window in DMS acts like an agent-key tap: the
// daemon's agent-tap brings it to the focused workspace, or parks it again
// when it already had focus. While the daemon is offline, clicks focus it
// as DMS normally would.
Item {
    id: root
    property string pluginId: ""
    property var pluginService: null
    property var wrapped: []
    // Shared with every replacement callback, including ones on snapshots an
    // open menu still holds after a refresh; they outlive this item. TITLE is
    // the daemon's agent window title, ONLINE whether it takes agent-tap.
    property var hooks: ({active: true, online: false, title: ""})
    property int requestId: 0

    // On niri, the dock (clicks and its window list) and the bar's RunningApps
    // and AppsDock widgets (clicks, scroll cycling) activate windows through
    // CompositorService's mutable JS snapshots. Replace only the agent's
    // activation callback, before activation can switch to its parking
    // workspace. Paths that bypass the snapshots still follow it to its
    // workspace: workspace-switcher window icons (NiriService.focusWindow)
    // and the AppsDock widget's window list (raw Wayland toplevels). No
    // compositor focus watcher or changes to the installed DMS files.
    // A refresh leaves superseded snapshots hooked: an open dock menu keeps
    // the snapshots it opened with. Unloading turns every hook off.
    function install() {
        const hooks = root.hooks;
        if (!CompositorService.isNiri || !hooks.title) {
            wrapped = [];
            return;
        }
        const windows = [];
        for (const window of CompositorService.sortedToplevels) {
            if (window.appId !== "com.mitchellh.ghostty"
                    || window.title !== hooks.title
                    || window.niriWindowId === undefined)
                continue;
            if (window.activate.hooks !== hooks) {
                const original = window.activate;
                const title = window.title;
                const activate = function () {
                    return hooks.active && hooks.online && hooks.title === title ? root.tap() : original();
                };
                activate.hooks = hooks;
                activate.original = original;
                window.activate = activate;
            }
            windows.push(window);
        }
        wrapped = windows;
    }

    function unhook() {
        hooks.active = false;
        for (const window of wrapped) {
            if (window.activate.hooks === hooks)
                window.activate = window.activate.original;
        }
        wrapped = [];
    }

    function tap() {
        requestId++;
        control.send({command: "agent-tap", id: requestId});
        replyTimeout.restart();
    }

    function receive(data) {
        if (data.type === "status") {
            heartbeat.restart();
            hooks.online = data.protocol_version === 1 && typeof data.agent_title === "string"
                && (data.capabilities || []).includes("agent-tap");
            if (hooks.online && data.agent_title !== hooks.title) {
                hooks.title = data.agent_title;
                install();
            }
        } else if (data.type === "reply" && data.id === requestId) {
            replyTimeout.stop();
            if (data.error)
                ToastService.showError("Hermes sidebar", data.error);
        }
    }

    Connections {
        target: CompositorService
        function onSortedToplevelsChanged() { root.install(); }
        function onIsNiriChanged() { root.install(); }
    }

    DankSocket {
        id: control
        path: Quickshell.env("XDG_RUNTIME_DIR") + "/voicekey/control.sock"
        connected: true
        onConnectionStateChanged: {
            if (!linkUp)
                root.hooks.online = false;
        }
        parser: SplitParser {
            onRead: message => {
                try {
                    root.receive(JSON.parse(message));
                } catch (e) {
                    console.warn("hermesSidebar: invalid message from Voicekey:", e);
                }
            }
        }
    }

    // Status arrives about every two seconds; silence means a stuck link.
    Timer {
        id: heartbeat
        interval: 6000
        onTriggered: {
            root.hooks.online = false;
            control.connected = false;
            Qt.callLater(() => { control.connected = true; });
        }
    }
    Timer {
        id: replyTimeout
        interval: 5000
        onTriggered: ToastService.showError("Hermes sidebar", "Voicekey did not answer the click")
    }

    IpcHandler {
        target: "hermes-sidebar"
        function status(): string {
            return JSON.stringify({online: root.hooks.online, title: root.hooks.title,
                                   hookedWindows: root.wrapped.map(w => w.niriWindowId)});
        }
    }

    Component.onCompleted: install()
    Component.onDestruction: unhook()
}
