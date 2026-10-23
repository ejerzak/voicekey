import QtQuick
import Quickshell
import Quickshell.Io
import qs.DCommon.Common
import qs.Services

// Clicking an agent window in DMS acts like an agent-key tap for that profile: the
// daemon's agent-tap brings it to the focused workspace, or parks it again
// when it already had focus. While the daemon is offline, clicks focus it
// as DMS normally would.
Item {
    id: root
    property string pluginId: ""
    property var pluginService: null
    property var wrapped: []
    // Shared with every replacement callback, including ones on snapshots an
    // open menu still holds after a refresh; they outlive this item. Profiles
    // contain only window identities; ONLINE says whether agent-tap is supported.
    property var hooks: ({active: true, online: false, profiles: []})
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
        if (!CompositorService.isNiri || !hooks.profiles.length) {
            wrapped = [];
            return;
        }
        const windows = [];
        for (const window of CompositorService.sortedToplevels) {
            const profile = profileFor(window.appId, window.title);
            if (!profile || window.niriWindowId === undefined)
                continue;
            if (window.activate.hooks !== hooks || window.activate.profileId !== profile.id) {
                const original = window.activate.hooks === hooks
                    ? window.activate.original : window.activate;
                const appId = window.appId;
                const title = window.title;
                const profileId = profile.id;
                const activate = function () {
                    const current = hooks.profiles.find(p => p.app_id === appId
                        && p.terminal_title === title && p.id === profileId);
                    return hooks.active && hooks.online && current ? root.tap(profileId) : original();
                };
                activate.hooks = hooks;
                activate.profileId = profileId;
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

    function profileFor(appId, title) {
        return hooks.profiles.find(p => p.app_id === appId && p.terminal_title === title);
    }

    function tap(profileId) {
        requestId++;
        const request = {command: "agent-tap", id: requestId};
        if (profileId) request.args = {profile: profileId};
        control.send(request);
        replyTimeout.restart();
    }

    function receive(data) {
        if (data.type === "status") {
            heartbeat.restart();
            hooks.online = data.protocol_version === 1
                && (data.capabilities || []).includes("agent-tap");
            if (hooks.online) {
                if ((data.capabilities || []).includes("agent-profiles")
                        && Array.isArray(data.agent_profiles)) {
                    hooks.profiles = data.agent_profiles.filter(p => typeof p.id === "string"
                        && typeof p.app_id === "string" && typeof p.terminal_title === "string");
                } else {
                    hooks.profiles = typeof data.agent_title === "string"
                        ? [{id: "", app_id: "dev.voicekey.Hermes", terminal_title: data.agent_title}] : [];
                }
                install();
            }
        } else if (data.type === "reply" && data.id === requestId) {
            replyTimeout.stop();
            if (data.error)
                ToastService.showError("Agent sidebar", data.error);
        }
    }

    Connections {
        target: CompositorService
        function onSortedToplevelsChanged() { root.install(); }
        function onIsNiriChanged() { root.install(); }
    }

    DSocket {
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
        onTriggered: ToastService.showError("Agent sidebar", "Voicekey did not answer the click")
    }

    IpcHandler {
        target: "hermes-sidebar"
        function status(): string {
            return JSON.stringify({online: root.hooks.online, profiles: root.hooks.profiles,
                                   hookedWindows: root.wrapped.map(w => w.niriWindowId)});
        }
    }

    Component.onCompleted: install()
    Component.onDestruction: unhook()
}
