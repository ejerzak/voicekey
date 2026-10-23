pragma Singleton
import Quickshell

// Mock of DMS's CompositorService on niri: window snapshots whose stock
// activate() records the window it would focus.
Singleton {
    property bool isNiri: true
    property var sortedToplevels: []
    property var focused: []

    function snapshot(id, title, appId) {
        return {appId: appId || "dev.voicekey.Hermes", title: title, niriWindowId: id,
                activate: function () { focused.push(id); }};
    }
}
