var video = document.getElementById('video');

var sendCanvas = document.createElement('canvas');
const width = 640;
const height = 480;
sendCanvas.width = width;
sendCanvas.height = height;
var sendCtx = sendCanvas.getContext('2d');

var wsProto = location.protocol === 'https:' ? 'wss://' : 'ws://';
var ws = new WebSocket(wsProto + location.host + '/ws');
var pending = false;
var fallbackTimeout;

// Reachy POV MJPEG stream (simulation container, host port 5001)
document.getElementById('pov').src =
    location.protocol + '//' + location.hostname + ':5001/stream';

var banner = document.getElementById('banner');
var connStat = document.getElementById('conn-stat');
var gestureStat = document.getElementById('gesture-stat');
var armStat = document.getElementById('arm-stat');
var resetBtn = document.getElementById('resetBtn');
var chips = {
    red: document.getElementById('chip-red'),
    green: document.getElementById('chip-green'),
    blue: document.getElementById('chip-blue'),
};

resetBtn.onclick = function () {
    if (ws.readyState === 1) {
        ws.send(JSON.stringify({ t: 'reset' }));
        banner.textContent = 'resetting the scene…';
    }
};

var recordBtn = document.getElementById('recordBtn');
var isRecording = false;
recordBtn.onclick = function () {
    if (ws.readyState !== 1) return;
    // optimistic; the status feed confirms the real state
    ws.send(JSON.stringify({ t: 'record', action: isRecording ? 'stop' : 'start' }));
};

function renderRecording(rec) {
    isRecording = !!rec;
    recordBtn.classList.toggle('recording', isRecording);
    recordBtn.textContent = isRecording ? '■ Stop' : '● Record';
}

navigator.mediaDevices.getUserMedia({ video: true })
    .then(function (s) { video.srcObject = s; })
    .catch(function (e) { connStat.textContent = 'webcam error: ' + e.name; });

function sendFrame() {
    if (ws.readyState === 1) {
        sendCtx.drawImage(video, 0, 0, width, height);
        sendCanvas.toBlob(function (b) {
            if (b) {
                ws.send(b);
                pending = true;
                clearTimeout(fallbackTimeout);
                fallbackTimeout = setTimeout(function () {
                    pending = false;
                    sendFrame();
                }, 150);
            }
        }, 'image/jpeg', 0.5);
    } else {
        setTimeout(sendFrame, 30);
    }
}

ws.onopen = function () {
    connStat.textContent = 'connected';
    sendFrame();
};

ws.onclose = function () {
    connStat.textContent = 'disconnected';
};

var topicsBody = document.getElementById('topics-body');

function renderTopics(topics) {
    if (!topics || !topics.length) return;
    var rows = '';
    for (var i = 0; i < topics.length; i++) {
        var t = topics[i];
        var live = t.hz > 0.05;
        rows += '<tr>'
            + '<td class="mono">' + t.n + '</td>'
            + '<td class="' + (live ? 'hz-live' : 'muted') + '">'
            + (live ? t.hz.toFixed(1) + ' Hz' : 'idle') + '</td>'
            + '<td class="mono">' + (t.v || '-') + '</td>'
            + '<td class="muted">' + t.d + '</td>'
            + '</tr>';
    }
    topicsBody.innerHTML = rows;
}

function updateStatus(d) {
    if (d.msg) {
        banner.textContent = d.msg;
        banner.classList.toggle('picking', !!d.arm && d.arm.indexOf('PICK') === 0);
    }
    if (d.topics) renderTopics(d.topics);
    renderRecording(d.recording);
    for (var c in chips) {
        chips[c].classList.toggle('gazed', d.zone === c);
        chips[c].classList.toggle('selected', d.selected === c);
    }
    gestureStat.textContent = 'gesture: ' + (d.gesture || '—');
    gestureStat.classList.toggle('active', d.gesture === 'HAND_RAISED');
    armStat.textContent = 'arm: ' + (d.arm || '—');
    armStat.classList.toggle('active', !!d.arm && d.arm !== 'IDLE');
    connStat.textContent = d.gaze_fresh
        ? ('gaze yaw ' + d.yaw_deg + '°')
        : 'no face detected';
}

ws.onmessage = function (e) {
    try {
        var m = JSON.parse(e.data);
        if (m.t === "gaze") {
            // gaze replies pace the webcam upload loop: send the next frame
            // as soon as perception finished with the previous one
            if (pending) {
                pending = false;
                clearTimeout(fallbackTimeout);
                requestAnimationFrame(sendFrame);
            }
        } else if (m.t === "status") {
            updateStatus(m.d);
        }
    } catch (x) { }
};
