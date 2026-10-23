"""Local status/control socket. Mutations run on the keyboard controller thread.

The DMS widget subscribes to status; no model imports, polling subprocesses or
second copy of dictation state are needed in the shell. Slow clients are dropped.
"""
from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import selectors
import socket
import struct
import threading
import time

PROTOCOL_VERSION = 1
CAPABILITIES = ['capture-client-name', 'editor-focus', 'capture-preview', 'draft-toggle', 'agent-tap', 'agent-profiles']
CAPTURE_COMMANDS = ('capture-start', 'capture-finish', 'capture-cancel')
COMMANDS = ('status', 'start', 'start-typing', 'stop', 'cancel', 'draft-on', 'draft-off', 'pause-on-switch', 'follow-focus', 'pin', 'free-memory', 'editor-focus', 'agent-tap', 'agent-select') + CAPTURE_COMMANDS


def socket_path():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'voicekey' / 'control.sock'


@dataclass
class Receipt:
    done: threading.Event = field(default_factory=threading.Event)
    sent: bool = False


class ControlServer:
    def __init__(self, path=None, *, editor_focus=None):
        self.editor_focus = editor_focus
        self.path = Path(path) if path else socket_path()
        self.commands = queue.Queue(maxsize=32)
        self.responses = queue.SimpleQueue()
        self.status = {'state': 'loading', 'listening': False}
        self.closed = threading.Event()
        self.thread = None
        self.listener = None
        # Readable once a command is queued. The keyboard thread drains
        # commands between key events, and waits on this so a dock click
        # need not wait for the next key or its one-second timeout.
        self.wake = self._waker = None

    def start(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse to unlink a live server (e.g. a second daemon invocation).
        if self.path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.2)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    self.path.unlink()
                else:
                    raise OSError('Voicekey control socket is already in use')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            self.listener.listen(8)
            self.listener.setblocking(False)
        except Exception:
            self.listener.close()
            self.listener = None
            raise
        self.wake, self._waker = socket.socketpair()
        self.wake.setblocking(False)
        self._waker.setblocking(False)
        self.thread = threading.Thread(target=self._run, name='voicekey-control', daemon=True)
        self.thread.start()

    def publish(self, status):
        self.status = dict(status)

    def emit(self, client, message, *, deadline=None, cancelled=None):
        """Queue a private event. Receipt confirms transport write, not consumption."""
        receipt = Receipt()
        if self.closed.is_set():
            receipt.done.set()
        else:
            self.responses.put((client, message, receipt, deadline, cancelled))
        return receipt

    def drain(self, handler):
        for _ in range(32):
            try:
                client, message, deadline = self.commands.get_nowait()
            except queue.Empty:
                break
            error = None
            extra = {}
            if time.monotonic() > deadline or client.fileno() < 0:
                error = 'Control request expired'
            else:
                try:
                    result = handler(message['command'], args=message.get('args', {}),
                                     client=client, request_id=message.get('id'))
                    if result:
                        extra.update(result)
                except (ValueError, RuntimeError) as exc:
                    error = str(exc)
            self.emit(client, {'type': 'reply', 'id': message.get('id'), 'error': error, **extra})

    def _run(self):
        clients = {}
        outputs = {}
        with selectors.DefaultSelector() as selector:
            selector.register(self.listener, selectors.EVENT_READ)

            def drop(client):
                if client in clients:
                    selector.unregister(client)
                    clients.pop(client)
                    for _, _, receipt, _, _ in outputs.pop(client):
                        receipt.done.set()
                    client.close()

            def send(client, message, receipt=None, deadline=None, cancelled=None):
                receipt = receipt or Receipt()
                if client not in clients:
                    receipt.done.set()
                    return
                data = (json.dumps(message, ensure_ascii=True) + '\n').encode()
                pending = outputs[client]
                if sum(len(data) - offset for data, offset, *_ in pending) + len(data) > 1024 * 1024:
                    drop(client)
                    receipt.done.set()
                    return
                pending.append((data, 0, receipt, deadline or time.monotonic() + 3, cancelled))
                selector.modify(client, selectors.EVENT_READ | selectors.EVENT_WRITE)

            previous, heartbeat = None, 0.0
            try:
                while not self.closed.is_set():
                    for key, mask in selector.select(.05):
                        client = key.fileobj
                        if client is self.listener:
                            client, _ = self.listener.accept()
                            # Permissions plus peer credentials: even a permissive
                            # pre-existing runtime directory cannot admit another UID.
                            _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                            if uid != os.getuid() or len(clients) >= 16:
                                client.close()
                                continue
                            client.setblocking(False)
                            clients[client] = b''
                            outputs[client] = deque()
                            selector.register(client, selectors.EVENT_READ)
                            send(client, {'type': 'status', 'protocol_version': PROTOCOL_VERSION,
                                          'capabilities': CAPABILITIES, **self.status})
                            continue
                        try:
                            if mask & selectors.EVENT_READ:
                                data = client.recv(4096)
                                if not data or len(clients[client]) + len(data) > 8192:
                                    drop(client)
                                    continue
                                clients[client] += data
                                while client in clients and b'\n' in clients[client]:
                                    line, clients[client] = clients[client].split(b'\n', 1)
                                    message = json.loads(line)
                                    if not isinstance(message, dict) or message.get('command') not in COMMANDS:
                                        raise ValueError('Unknown control command')
                                    if not isinstance(message.get('args', {}), dict):
                                        raise ValueError('args must be an object')
                                    if message.get('id') is not None and (isinstance(message['id'], bool) or
                                            not isinstance(message['id'], (str, int))):
                                        raise ValueError('id must be a string or integer')
                                    if message['command'] == 'editor-focus':
                                        args = message.get('args', {})
                                        peer_pid, _, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                                        if (set(args) != {'pid', 'server', 'focused', 'sequence'}
                                                or type(args['pid']) is not int or args['pid'] != peer_pid
                                                or not isinstance(args['server'], str) or len(args['server']) > 4096
                                                or type(args['focused']) is not bool
                                                or type(args['sequence']) is not int or args['sequence'] < 1):
                                            raise ValueError('Invalid editor focus event')
                                        # Unlike controller commands this only marks a focus boundary.
                                        # Do it on receipt, even while evdev select is asleep.
                                        if self.editor_focus is not None:
                                            self.editor_focus(args)
                                        continue
                                    if message['command'] == 'status':
                                        send(client, {'type': 'reply', 'id': message.get('id'),
                                             'error': 'status takes no arguments' if message.get('args') else None,
                                             'protocol_version': PROTOCOL_VERSION, 'capabilities': CAPABILITIES, **self.status})
                                    else:
                                        self.commands.put_nowait((client, message, time.monotonic() + 2))
                                        try:
                                            self._waker.send(b'\0')
                                        except BlockingIOError:
                                            pass  # a wake is already pending
                            if client in clients and mask & selectors.EVENT_WRITE:
                                data, offset, receipt, deadline, cancelled = outputs[client][0]
                                if time.monotonic() >= deadline or cancelled is not None and cancelled.is_set():
                                    drop(client)
                                    continue
                                count = client.send(data[offset:])
                                if not count:
                                    drop(client)
                                elif offset + count == len(data):
                                    outputs[client].popleft()
                                    receipt.sent = True
                                    receipt.done.set()
                                    if not outputs[client]:
                                        selector.modify(client, selectors.EVENT_READ)
                                else:
                                    outputs[client][0] = (data, offset + count, receipt, deadline, cancelled)
                        except BlockingIOError:
                            pass
                        except (OSError, ValueError, RecursionError, queue.Full):
                            drop(client)
                    while not self.responses.empty():
                        send(*self.responses.get_nowait())
                    for client, pending in tuple(outputs.items()):
                        if pending and time.monotonic() >= pending[0][3]:
                            drop(client)
                    status = self.status
                    if status != previous or time.monotonic() - heartbeat > 2:
                        for client in tuple(clients):
                            send(client, {'type': 'status', 'protocol_version': PROTOCOL_VERSION,
                                          'capabilities': CAPABILITIES, **status})
                        previous, heartbeat = status, time.monotonic()
            finally:
                for client in tuple(clients):
                    drop(client)
                while not self.responses.empty():
                    self.responses.get_nowait()[2].done.set()

    def close(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(1)
        if self.listener is not None:
            self.listener.close()
            self.path.unlink(missing_ok=True)
            self.listener = None
        for end in (self.wake, self._waker):
            if end is not None:
                end.close()


def request(command, path=None, *, args=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path or socket_path()))
        client.sendall((json.dumps({'command': command, 'id': 1, 'args': args or {}}) + '\n').encode())
        with client.makefile('rb') as stream:
            while line := stream.readline(65536):
                result = json.loads(line)
                if result.get('type') == 'reply':
                    return result
    raise OSError('Voicekey disconnected before replying')
