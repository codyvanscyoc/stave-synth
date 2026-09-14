"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "ui", "script.js"), "utf8");
const urlStart = source.indexOf("    const runtimeWsPort");
const urlEnd = source.indexOf("    // Exponential reconnect", urlStart);
assert.notEqual(urlStart, -1, "runtime URL section start marker must exist");
assert.notEqual(urlEnd, -1, "runtime URL section end marker must exist");
const urlSection = source.slice(urlStart, urlEnd);
const start = source.indexOf("    function connectWS()");
const end = source.indexOf("    // rAF-coalesced control resync", start);
assert.notEqual(start, -1, "connection section start marker must exist");
assert.notEqual(end, -1, "connection section end marker must exist");
const connectionSection = source.slice(start, end);

function resolvedUrl(runtime) {
    const context = vm.createContext({
        Number,
        window: {
            location: { hostname: "stave.test" },
            STAVE_RUNTIME: runtime,
        },
    });
    vm.runInContext(urlSection + "\nglobalThis.resolved = WS_URL;", context,
        { filename: "ui/script.js#runtime-url" });
    return context.resolved;
}

assert.equal(resolvedUrl({ websocket_port: 9876, instance: "test" }),
    "ws://stave.test:9876", "runtime WebSocket port is honored");
assert.equal(resolvedUrl(undefined), "ws://stave.test:8765",
    "missing runtime config uses compatibility port");
assert.equal(resolvedUrl({ websocket_port: 70000 }), "ws://stave.test:8765",
    "out-of-range runtime port uses compatibility port");

function harness() {
    let nextTimer = 1;
    const timeouts = new Map();
    const intervals = new Map();

    class FakeWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;
        static instances = [];

        constructor(url) {
            this.url = url;
            this.readyState = FakeWebSocket.CONNECTING;
            this.sent = [];
            this.closeCount = 0;
            FakeWebSocket.instances.push(this);
        }

        send(data) {
            if (this.readyState !== FakeWebSocket.OPEN) throw new Error("not open");
            this.sent.push(JSON.parse(data));
        }

        close() {
            this.closeCount++;
            this.readyState = FakeWebSocket.CLOSED;
        }
    }

    const context = vm.createContext({
        WebSocket: FakeWebSocket,
        Map,
        Math,
        JSON,
        console,
        WS_URL: "ws://test.invalid:8765",
        RECONNECT_DELAYS_MS: [1000, 2000, 4000, 8000, 15000],
        MAX_PENDING_QUEUE: 100,
        ws: null,
        wsReconnectAttempt: 0,
        wsReconnectTimer: null,
        wsCountdownTimer: null,
        wsPendingQueue: [],
        wsPendingByKey: new Map(),
        state: null,
        lastAudioHeartbeatMs: 123,
        audioIndicator: { className: "down" },
        statusIndicator: { textContent: "DISC", className: "" },
        handleServerMessage() {},
        setTimeout(fn) {
            const id = nextTimer++;
            timeouts.set(id, fn);
            return id;
        },
        clearTimeout(id) { timeouts.delete(id); },
        setInterval(fn) {
            const id = nextTimer++;
            intervals.set(id, fn);
            return id;
        },
        clearInterval(id) { intervals.delete(id); },
    });
    vm.runInContext(connectionSection, context, { filename: "ui/script.js#connection" });
    return { context, FakeWebSocket, timeouts, intervals };
}

function messages(socket) {
    return socket.sent.map((item) => item.type);
}

{
    const h = harness();
    vm.runInContext("connectWS()", h.context);
    const oldSocket = h.FakeWebSocket.instances[0];
    vm.runInContext("reconnectNow()", h.context);
    const currentSocket = h.FakeWebSocket.instances[1];

    assert.equal(oldSocket.closeCount, 1, "manual reconnect retires prior socket");
    oldSocket.onopen();
    assert.deepEqual(messages(oldSocket), [], "retired open does not hydrate");

    currentSocket.readyState = h.FakeWebSocket.OPEN;
    currentSocket.onopen();
    assert.deepEqual(messages(currentSocket), ["get_state", "get_cc_map"]);
    currentSocket.onopen();
    assert.deepEqual(messages(currentSocket), ["get_state", "get_cc_map"],
        "one socket hydrates only once");

    oldSocket.onerror();
    oldSocket.onclose();
    assert.equal(currentSocket.readyState, h.FakeWebSocket.OPEN,
        "retired error cannot close current socket");
    assert.equal(h.timeouts.size, 0, "retired close cannot schedule retry");
}

{
    const h = harness();
    vm.runInContext("connectWS()", h.context);
    const socket = h.FakeWebSocket.instances[0];
    socket.onclose();
    socket.onclose();
    assert.equal(h.timeouts.size, 1, "repeated close schedules one retry");

    const retry = [...h.timeouts.values()][0];
    h.timeouts.clear();
    retry();
    assert.equal(h.FakeWebSocket.instances.length, 2, "retry creates one replacement");
}

{
    const h = harness();
    vm.runInContext("connectWS()", h.context);
    const socket = h.FakeWebSocket.instances[0];
    vm.runInContext(`
        send({type: "panic"});
        send({type: "preset_load", slot: 2});
        send({type: "record_toggle"});
        send({type: "fader", id: 0, alt: 0, value: 0.1});
        send({type: "fader", id: 0, alt: 0, value: 0.8});
        send({type: "setting", section: "master", param: "bpm", value: 110});
        send({type: "macro_value", idx: 1, value: 0.4});
    `, h.context);
    socket.readyState = h.FakeWebSocket.OPEN;
    socket.onopen();

    assert.deepEqual(messages(socket), [
        "fader", "setting", "macro_value", "get_state", "get_cc_map",
    ], "only collapsed absolute values replay before hydration");
    assert.equal(socket.sent[0].value, 0.8, "latest fader value wins offline");
}

console.log("UI connection tests passed");
