import QtQuick

// Mock of DMS's DSocket: records sends; tests feed lines to the parser.
Item {
    property string path: ""
    property var parser: null
    property bool connected: false
    property bool linkUp: true
    property var sent: []
    signal connectionStateChanged

    function send(data) { sent.push(data); }
}
