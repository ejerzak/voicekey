import QtQuick
import Quickshell

// Load the complete widget with the same cache-busting URL used by DMS.
ShellRoot {
    id: shell
    property int failures: 0
    function check(name, ok) {
        console.warn((ok ? "PASS " : "FAIL ") + name);
        if (!ok) failures++;
    }
    function find(item, name) {
        if (item.objectName === name) return item;
        for (const child of item.children || []) {
            const match = find(child, name);
            if (match) return match;
        }
        return null;
    }
    function socketOf(plugin) {
        for (const child of plugin.children) {
            if (child.sent !== undefined) return child;
        }
        return null;
    }
    Timer {
        interval: 10
        running: true
        onTriggered: {
            const component = Qt.createComponent(Qt.resolvedUrl("Voicekey.qml") + "?revision=1",
                                                  Component.PreferSynchronous);
            check("the complete widget loads at a revision URL", component.status === Component.Ready);
            if (component.status !== Component.Ready) {
                console.warn(component.errorString());
                console.warn("DONE failures=" + shell.failures);
                Qt.quit();
                return;
            }
            const widget = component.createObject(shell);
            check("the complete widget instantiates", widget !== null);
            if (!widget) { Qt.quit(); return; }
            const socket = socketOf(widget);
            const status = {type: "status", state: "idle", capabilities: ["agent-profiles"],
                            agent_profile: "hermes", agent_profiles: [
                                {id: "hermes", name: "Hermes"},
                                {id: "codex-mail", name: "Codex mail"}]};
            socket.parser.read(JSON.stringify(status));
            const panel = widget.popoutContent.createObject(shell);
            check("the real widget panel instantiates", panel !== null);
            const choices = find(panel, "agentProfiles");
            const hermes = find(panel, "agentProfile-hermes");
            const codex = find(panel, "agentProfile-codex-mail");
            check("selector displays profile names and the current choice", choices && choices.visible
                  && hermes && codex && hermes.text === "Hermes" && codex.text === "Codex mail"
                  && hermes.iconName === "radio_button_checked" && !hermes.enabled && codex.enabled);
            codex.clicked();
            const sent = socket.sent[socket.sent.length - 1];
            check("selector sends agent-select with the chosen ID", sent.command === "agent-select"
                  && sent.args.profile === "codex-mail" && widget.status.agent_profile === "hermes");
            check("selector waits for acknowledgement", widget.requestPending && !codex.enabled);
            socket.parser.read(JSON.stringify({type: "reply", id: sent.id, error: null}));
            status.agent_profile = "codex-mail";
            socket.parser.read(JSON.stringify(status));
            check("selector reflects daemon-confirmed selection", codex.iconName === "radio_button_checked"
                  && !codex.enabled && hermes.enabled);
            status.capabilities = [];
            socket.parser.read(JSON.stringify(status));
            check("selector hides for older daemons", !choices.visible);
            console.warn("DONE failures=" + shell.failures);
            Qt.quit();
        }
    }
}
