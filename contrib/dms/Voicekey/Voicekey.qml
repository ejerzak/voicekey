import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.DCommon.Common as DCommon
import qs.Services
import qs.DCommon.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root
    layerNamespacePlugin: "voicekey"
    popoutWidth: 340

    property var status: ({state: "offline", listening: false})
    property string controlError: ""
    property bool requestPending: false
    property int requestId: 0
    property string serviceState: "unknown"
    property string serviceAction: ""
    property bool servicePending: false
    property bool serviceQueryAgain: false
    property string startCommand: "start"
    readonly property bool disabled: !online && serviceState === "inactive"
    readonly property bool starting: servicePending && serviceAction === "start"
    readonly property bool stopping: servicePending && serviceAction === "stop"
    readonly property bool canStart: idle || (online && status.state === "unloaded")
    readonly property bool controlsBusy: requestPending || servicePending
    readonly property bool online: control.linkUp && status.state !== "offline"
    readonly property bool listening: online && status.listening === true
    readonly property bool idle: online && (status.state === "idle" || status.state === "paused")
    readonly property bool canToggleDraft: online && Array.isArray(status.capabilities)
        && status.capabilities.indexOf("draft-toggle") !== -1
    readonly property bool muted: AudioService.source?.audio?.muted ?? false
    readonly property bool noMicrophone: !AudioService.source
    readonly property string stateText: {
        if (stopping) return "Disabling VoiceKey…";
        if (starting && !online) return "Starting VoiceKey…";
        if (disabled) return "VoiceKey disabled";
        if (!online) return "VoiceKey unavailable";
        if (listening && noMicrophone) return "Listening · no microphone";
        if (listening && muted) return "Listening · microphone muted";
        if (listening && status.models === "loading") return "Listening · loading models";
        if (listening && status.binding === true) return "Listening…";
        if (listening && status.draft_mode) return "Draft → " + (status.destination_name || "editor");
        if (status.draft_waiting) return "Draft ready · microphone off";
        if (listening) return status.destination_name
            ? "Listening → " + status.destination_name : "Listening…";
        if (status.state === "finishing") return "Finishing dictation";
        if (status.state === "paused") return "Off · " + status.pause_reason;
        if (status.state === "loading") return "Loading models";
        if (status.state === "unloading") return "Freeing memory…";
        if (status.state === "unloaded") return "Models unloaded · ready on next use";
        if (status.state === "unavailable") return "Dictation unavailable";
        return "Dictation off";
    }
    readonly property color stateColor: !online ? Theme.surfaceVariantText
        : listening ? ((muted || noMicrophone) ? Theme.error : Theme.primary)
        : status.state === "finishing" ? Theme.secondary : Theme.surfaceVariantText
    readonly property string stateIcon: !online ? "mic_off"
        : listening ? ((muted || noMicrophone) ? "mic_off" : "mic")
        : status.state === "finishing" ? "hourglass_top"
        : status.state === "paused" ? "mic_off" : "mic_none"

    function send(command, args) {
        if (!control.linkUp || controlsBusy) return;
        requestPending = true;
        controlError = "";
        requestId++;
        control.send({command: command, id: requestId, args: args || {}});
        requestTimeout.restart();
    }

    function checkService() {
        if (serviceQuery.running) serviceQueryAgain = true;
        else serviceQuery.running = true;
    }

    function setService(action) {
        if (servicePending) return;
        startAfterClose.stop();
        serviceAction = action;
        servicePending = true;
        serviceState = "unknown";
        controlError = "";
        serviceCommand.command = ["systemctl", "--user", action, "voicekey.service"];
        serviceCommand.running = true;
        serviceTimeout.restart();
    }

    Component.onCompleted: checkService()

    Process {
        id: serviceQuery
        command: ["systemctl", "--user", "show", "voicekey.service", "--property=ActiveState", "--value"]
        stdout: StdioCollector { id: serviceOutput }
        onExited: exitCode => {
            root.serviceState = exitCode === 0 ? serviceOutput.text.trim() : "unknown";
            if (root.serviceQueryAgain) {
                root.serviceQueryAgain = false;
                Qt.callLater(root.checkService);
            }
        }
    }
    Process {
        id: serviceCommand
        stderr: StdioCollector { id: serviceErrors }
        onExited: exitCode => {
            serviceTimeout.stop();
            root.servicePending = false;
            if (exitCode !== 0)
                root.controlError = serviceErrors.text.trim() || "Could not change VoiceKey service state";
            root.checkService();
        }
    }
    Timer {
        id: serviceTimeout
        interval: 35000
        onTriggered: {
            root.servicePending = false;
            root.controlError = "Service change not confirmed; check VoiceKey service status";
            if (serviceCommand.running) serviceCommand.signal(15);
            root.checkService();
        }
    }

    DCommon.DSocket {
        id: control
        path: Quickshell.env("XDG_RUNTIME_DIR") + "/voicekey/control.sock"
        connected: true
        onConnectionStateChanged: {
            if (!linkUp) {
                root.status = {state: "offline", listening: false};
                root.requestPending = false;
                requestTimeout.stop();
                root.serviceState = "unknown";
                root.checkService();
            }
        }
        parser: SplitParser {
            onRead: message => {
                try {
                    const data = JSON.parse(message);
                    if (data.type === "status") {
                        root.status = data;
                        heartbeat.restart();
                    } else if (data.type === "reply" && data.id === root.requestId) {
                        root.requestPending = false;
                        root.controlError = data.error || "";
                        requestTimeout.stop();
                    }
                } catch (e) {
                    root.controlError = "Invalid response from Voicekey";
                }
            }
        }
    }

    Timer {
        id: heartbeat
        interval: 6000
        onTriggered: {
            control.connected = false;
            Qt.callLater(() => { control.connected = true; });
        }
    }
    Timer {
        id: requestTimeout
        interval: 3000
        onTriggered: {
            root.requestPending = false;
            root.controlError = "Voicekey did not acknowledge the request";
        }
    }
    Timer {
        id: startAfterClose
        interval: 250
        onTriggered: root.send(root.startCommand)
    }

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS
            DIcon {
                name: root.stateIcon
                size: root.iconSize
                color: root.stateColor
                anchors.verticalCenter: parent.verticalCenter
            }
            StyledText {
                visible: root.listening || root.status.draft_waiting || root.status.state === "paused"
                text: root.stateText
                width: Math.min(implicitWidth, 240)
                elide: Text.ElideRight
                color: root.stateColor
                font.pixelSize: Theme.fontSizeSmall
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }
    verticalBarPill: Component {
        DIcon {
            name: root.stateIcon
            size: root.iconSize
            color: root.stateColor
        }
    }

    popoutContent: Component {
        PopoutComponent {
            id: panel
            headerText: "Voicekey"
            detailsText: root.stateText
            showCloseButton: true

            Column {
                width: parent.width
                spacing: Theme.spacingS

                StyledText {
                    width: parent.width
                    text: root.disabled ? "VoiceKey is stopped. Enable it here to use dictation again."
                        : root.status.draft_waiting ? "Accept the draft to insert it into its original buffer, or discard it."
                        : root.status.binding ? "Binding destination…"
                        : root.status.draft_mode ? "Press the dictation key to accept the draft. Accept/cancel hotkeys act only in the original window."
                        : root.status.allow_typing ? "Simulated typing enabled for this session. Switching windows stops listening."
                        : root.status.state === "paused" ? "Microphone off. Focus a text field and tap to start again."
                        : root.status.tracking_notice ? root.status.tracking_notice
                        : root.listening ? "Ordinary dictation → " + (root.status.destination_name || "Waiting for a text field.")
                        : root.status.draft_enabled ? "Use drafts where supported; elsewhere, tap to keep listening or hold to talk."
                        : "Tap the dictation key to keep listening; hold to talk."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DButton {
                    width: parent.width
                    text: root.status.draft_mode ? "Accept draft" : root.listening ? "Stop listening" : "Start listening"
                    iconName: root.status.draft_mode ? "check" : root.listening ? "stop" : "mic"
                    enabled: !root.controlsBusy && (root.listening || root.canStart || root.status.draft_waiting)
                    onClicked: {
                        if (root.listening || root.status.draft_waiting) root.send("stop");
                        else {
                            // Restore application focus before the daemon binds it.
                            panel.closePopout();
                            root.startCommand = "start";
                            startAfterClose.restart();
                        }
                    }
                }
                DButton {
                    width: parent.width
                    visible: root.status.draft_mode === true
                    text: "Discard draft"
                    iconName: "close"
                    enabled: !root.controlsBusy
                    onClicked: root.send("cancel")
                }
                Repeater {
                    // This policy applies to ordinary sessions, including draft fallback.
                    model: [
                        {policy: "pause", label: "Pause on window switch", command: "pause-on-switch"},
                        {policy: "follow", label: "Follow focused window", command: "follow-focus"},
                        {policy: "pin", label: "Stay at original destination", command: "pin"}
                    ]
                    delegate: DButton {
                        required property var modelData
                        width: parent.width
                        text: modelData.label
                        iconName: (root.status.draft_mode || root.status.allow_typing ? "pause" : root.status.destination_policy) === modelData.policy
                            ? "radio_button_checked" : "radio_button_unchecked"
                        enabled: root.canStart && !root.controlsBusy
                            && (modelData.policy !== "follow" || root.status.can_follow === true)
                        onClicked: root.send(modelData.command)
                    }
                }
                StyledText {
                    width: parent.width
                    text: root.status.draft_mode
                        ? "Drafts stay in one Emacs or Neovim buffer. Window switches pause recording until you accept or discard."
                        : "Destination choices last until restart. Background dictation requires a supported destination, such as Emacs."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DButton {
                    width: parent.width
                    visible: root.canToggleDraft
                    text: root.status.draft_enabled ? "Draft mode: on" : "Draft mode: off"
                    iconName: root.status.draft_enabled ? "toggle_on" : "toggle_off"
                    enabled: root.canStart && !root.controlsBusy
                    onClicked: root.send(root.status.draft_enabled ? "draft-off" : "draft-on")
                }
                StyledText {
                    width: parent.width
                    visible: root.canToggleDraft
                    text: "Use drafts in supported Emacs and Neovim buffers; ordinary dictation elsewhere. This choice lasts until restart."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DButton {
                    width: parent.width
                    visible: root.status.state === "paused" && root.status.can_type === true
                    text: "Use simulated typing this session"
                    iconName: "keyboard"
                    enabled: root.canStart && !root.controlsBusy
                    onClicked: {
                        panel.closePopout();
                        root.startCommand = "start-typing";
                        startAfterClose.restart();
                    }
                }
                StyledText {
                    width: parent.width
                    visible: root.status.state === "paused" && root.status.can_type === true
                    text: "Try Start listening again once the field has focus. Use simulated typing only for unsupported fields; it can trigger shortcuts elsewhere."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                Column {
                    objectName: "agentProfiles"
                    width: parent.width
                    spacing: Theme.spacingS
                    visible: root.online && (root.status.capabilities || []).includes("agent-profiles")
                    StyledText {
                        width: parent.width
                        text: "Agent"
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                    }
                    Repeater {
                        model: root.status.agent_profiles || []
                        delegate: DButton {
                            required property var modelData
                            objectName: "agentProfile-" + modelData.id
                            width: parent.width
                            text: modelData.name
                            iconName: root.status.agent_profile === modelData.id
                                ? "radio_button_checked" : "radio_button_unchecked"
                            enabled: !root.controlsBusy && root.status.agent_profile !== modelData.id
                            onClicked: root.send("agent-select", {profile: modelData.id})
                        }
                    }
                    StyledText {
                        width: parent.width
                        text: "Select the agent for future key presses. This choice is remembered on this machine."
                        wrapMode: Text.Wrap
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                    }
                }
                DButton {
                    width: parent.width
                    text: root.status.unload_pending ? "Freeing memory…" : "Free memory"
                    iconName: "memory"
                    enabled: root.online && !root.controlsBusy && !root.status.unload_pending
                        && root.status.state !== "unloaded" && root.status.state !== "loading"
                    onClicked: root.send("free-memory")
                }
                StyledText {
                    width: parent.width
                    text: "Finishes pending speech and unloads models. The next dictation reloads them."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                DButton {
                    width: parent.width
                    text: root.stopping ? "Disabling…" : root.starting ? "Starting…"
                        : root.online || root.serviceState === "active" || root.serviceState === "activating"
                            ? "Disable VoiceKey" : "Enable VoiceKey"
                    iconName: "power_settings_new"
                    enabled: !root.servicePending && !serviceCommand.running
                    onClicked: root.setService(root.online || root.serviceState === "active"
                        || root.serviceState === "activating" ? "stop" : "start")
                }
                StyledText {
                    width: parent.width
                    text: "Disabling stops VoiceKey and its hotkeys for this login session. Other apps can still use the microphone."
                    wrapMode: Text.Wrap
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }
                StyledText {
                    width: parent.width
                    visible: root.muted || root.noMicrophone
                    text: root.noMicrophone ? "No microphone available" : "Microphone is muted"
                    color: Theme.error
                    font.pixelSize: Theme.fontSizeSmall
                }
                StyledText {
                    width: parent.width
                    visible: text.length > 0
                    text: root.controlError || root.status.error || ""
                    wrapMode: Text.Wrap
                    color: Theme.error
                    font.pixelSize: Theme.fontSizeSmall
                }
            }
        }
    }
}
