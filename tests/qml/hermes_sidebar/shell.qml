import QtQuick
import Quickshell
import qs.Services

// Drives contrib/dms/HermesSidebar against mock DMS services; the test runner
// copies the plugin beside this file. Prints PASS/FAIL lines, then DONE.
ShellRoot {
    id: shell
    property int failures: 0

    function check(name, ok) {
        console.warn((ok ? "PASS " : "FAIL ") + name);
        if (!ok)
            failures++;
    }

    function socketOf(plugin) {
        for (let i = 0; i < plugin.children.length; i++) {
            if (plugin.children[i].sent !== undefined)
                return plugin.children[i];
        }
        return null;
    }

    function status(socket, fields) {
        socket.parser.read(JSON.stringify(Object.assign(
            {type: "status", protocol_version: 1, capabilities: ["agent-tap"],
             agent_title: "Voicekey Hermes"}, fields || {})));
    }

    function taps(socket) {
        return tapsIn(socket.sent);
    }

    function tapsIn(sent) {
        return sent.filter(m => m.command === "agent-tap").length;
    }

    Component { id: plugin; HermesSidebar {} }

    Timer {
        interval: 10
        running: true
        onTriggered: shell.run()
    }

    function run() {
        const C = CompositorService;
        C.sortedToplevels = [C.snapshot(15, "Voicekey Hermes"), C.snapshot(19, "other shell")];
        let p = plugin.createObject(shell);
        let socket = socketOf(p);
        check("nothing is hooked before the daemon names its window", p.wrapped.length === 0
              && C.sortedToplevels[0].activate.hooks === undefined);

        status(socket);
        check("the daemon's window is hooked", JSON.stringify(p.wrapped.map(w => w.niriWindowId)) === "[15]");
        check("other Ghostty windows are untouched", C.sortedToplevels[1].activate.hooks === undefined);

        C.sortedToplevels[0].activate();
        check("a click sends agent-tap instead of focusing", taps(socket) === 1 && C.focused.length === 0);

        const menuHeld = C.sortedToplevels[0];          // what an open dock menu keeps
        C.sortedToplevels = [C.snapshot(15, "Voicekey Hermes"), C.snapshot(19, "renamed")];
        menuHeld.activate();
        check("a snapshot an open menu kept still taps after a refresh", taps(socket) === 2);

        const fresh = C.sortedToplevels[0];
        const hook = fresh.activate;
        C.isNiri = false; C.isNiri = true;               // a second install on the same snapshots
        check("a reinstall does not wrap twice", fresh.activate === hook);

        socket.parser.read(JSON.stringify({type: "reply", id: p.requestId, error: "Finish the current dictation"}));
        check("a refused tap shows the daemon's reason", ToastService.errors.indexOf("Finish the current dictation") >= 0);
        socket.parser.read(JSON.stringify({type: "reply", id: p.requestId, error: null, agent: "raised"}));
        socket.parser.read("not json");
        check("an accepted tap or a garbled line adds no toast", ToastService.errors.length === 1);

        socket.linkUp = false;
        socket.connectionStateChanged();
        fresh.activate();
        check("with the daemon offline a click focuses as DMS would", JSON.stringify(C.focused) === "[15]"
              && taps(socket) === 2);
        socket.linkUp = true;
        status(socket);
        fresh.activate();
        check("a status after reconnecting restores the tap", taps(socket) === 3);

        status(socket, {capabilities: []});
        fresh.activate();
        check("a daemon without agent-tap leaves clicks to DMS", JSON.stringify(C.focused) === "[15,15]");

        status(socket, {agent_title: "Agent"});
        C.sortedToplevels = [C.snapshot(15, "Voicekey Hermes"), C.snapshot(21, "Agent")];
        fresh.activate();
        check("a renamed agent window is hooked and the old title is not",
              JSON.stringify(p.wrapped.map(w => w.niriWindowId)) === "[21]"
              && JSON.stringify(C.focused) === "[15,15,15]" && taps(socket) === 3);
        const agent = C.sortedToplevels[1];
        agent.activate();
        check("the renamed window taps", taps(socket) === 4);

        const sent = socket.sent;                        // outlives the destroyed socket
        p.destroy();
        Qt.callLater(() => {
            check("unloading restores the current snapshot", agent.activate.hooks === undefined);
            menuHeld.activate();
            check("a kept snapshot falls back to focus after unloading",
                  JSON.stringify(C.focused) === "[15,15,15,15]" && tapsIn(sent) === 4);

            p = plugin.createObject(shell);              // reload
            socket = socketOf(p);
            status(socket, {agent_title: "Agent"});
            agent.activate();
            check("a reload hooks again", taps(socket) === 1);
            console.warn("DONE failures=" + failures);
            Qt.quit();
        });
    }
}
