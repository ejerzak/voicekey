pragma Singleton
import Quickshell

Singleton {
    property var errors: []
    function showError(message, details) { errors.push(details); }
}
