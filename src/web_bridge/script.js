var video = document.getElementById('video');

var sendCanvas = document.createElement('canvas');
const width = 640;
const height = 480;
sendCanvas.width = width;
sendCanvas.height = height;
var sendCtx = sendCanvas.getContext('2d');

var wsProto = location.protocol === 'https:' ? 'wss://' : 'ws://';
var ws = null;
var reconnectDelay = 500;
var pending = false;
var fallbackTimeout;

// Escape untrusted strings before they go into innerHTML. Topic names/descriptions
// and recording names are rendered this way; treat them as data, never markup.
function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
}

// Reachy POV MJPEG stream — proxied through this same origin (/pov/stream) so it
// works under HTTPS behind the reverse proxy instead of a hardcoded :5001 port.
document.getElementById('pov').src = location.origin + '/pov/stream';

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

var wasRecording = false;
function renderRecording(rec) {
    isRecording = !!rec;
    recordBtn.classList.toggle('recording', isRecording);
    recordBtn.textContent = isRecording ? '■ Stop' : '● Record';
    // when a recording finishes, refresh the list so the new file appears
    if (wasRecording && !isRecording) {
        setTimeout(loadRecordings, 800);
    }
    wasRecording = isRecording;
}

var recordingsBody = document.getElementById('recordings-body');

function humanSize(b) {
    if (b > 1048576) return (b / 1048576).toFixed(1) + ' MB';
    if (b > 1024) return (b / 1024).toFixed(0) + ' KB';
    return b + ' B';
}

function loadRecordings() {
    fetch('/recordings').then(function (r) { return r.json(); }).then(function (list) {
        if (!list.length) {
            recordingsBody.innerHTML =
                '<tr><td colspan="6" class="muted">no recordings yet — hit ● Record above</td></tr>';
            return;
        }
        var rows = '';
        for (var i = 0; i < list.length; i++) {
            var r = list[i];
            var url = location.origin + '/recordings/' + encodeURIComponent(r.name);
            var fox = 'https://app.foxglove.dev/~/view?ds=remote-file&ds.url='
                + encodeURIComponent(url);
            rows += '<tr>'
                + '<td class="mono">' + esc(r.name) + '</td>'
                + '<td>' + (r.duration_s != null ? r.duration_s + ' s' : '—') + '</td>'
                + '<td>' + (r.messages != null ? r.messages : '—') + '</td>'
                + '<td>' + (r.topics != null ? r.topics : '—') + '</td>'
                + '<td>' + humanSize(r.size_bytes) + '</td>'
                + '<td class="rec-actions">'
                + '<a href="' + esc(url) + '" download>Download</a>'
                + '<a href="' + esc(fox) + '" target="_blank" rel="noopener">Foxglove ↗</a>'
                + '<a href="#" class="del" data-name="' + esc(r.name) + '">Delete</a>'
                + '</td>'
                + '</tr>';
        }
        recordingsBody.innerHTML = rows;
    }).catch(function () { });
}

recordingsBody.addEventListener('click', function (e) {
    var el = e.target;
    if (!el.classList.contains('del')) return;
    e.preventDefault();
    var name = el.getAttribute('data-name');
    fetch('/recordings/' + encodeURIComponent(name), { method: 'DELETE' })
        .then(function () { loadRecordings(); })
        .catch(function () { });
});

loadRecordings();

navigator.mediaDevices.getUserMedia({ video: true })
    .then(function (s) { video.srcObject = s; })
    .catch(function (e) { connStat.textContent = 'webcam error: ' + e.name; });

function sendFrame() {
    if (!ws) return;
    if (ws.readyState === 1) {          // OPEN
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
    } else if (ws.readyState === 0) {   // CONNECTING — retry shortly
        setTimeout(sendFrame, 30);
    }
    // CLOSING/CLOSED: stop the pump; connect()'s onopen restarts it on reconnect
}

var topicsBody = document.getElementById('topics-body');
var topicLog = {};        // topic name -> [{ts, v}] rolling message stream
var topicExpanded = {};   // topic name -> bool
var topicsBuilt = false;

function pushLog(name, v) {
    var log = topicLog[name] || (topicLog[name] = []);
    if (log.length && log[log.length - 1].v === v) return;  // only on change
    log.push({ ts: new Date().toLocaleTimeString(), v: v });
    if (log.length > 15) log.shift();
}

function buildTopicsTable(topics) {
    var html = '';
    for (var i = 0; i < topics.length; i++) {
        var n = topics[i].n;
        html += '<tr class="topic-row" data-topic="' + esc(n) + '">'
            + '<td class="mono"><span class="caret">▸</span> ' + esc(n) + '</td>'
            + '<td class="rate"></td>'
            + '<td class="mono latest"></td>'
            + '<td class="muted">' + esc(topics[i].d) + '</td>'
            + '</tr>'
            + '<tr class="topic-detail" data-topic="' + esc(n) + '"><td colspan="4">'
            + '<pre class="stream"></pre></td></tr>';
    }
    topicsBody.innerHTML = html;
    topicsBuilt = true;
}

function renderTopics(topics) {
    if (!topics || !topics.length) return;
    if (!topicsBuilt || topicsBody.querySelectorAll('.topic-row').length !== topics.length) {
        buildTopicsTable(topics);
    }
    for (var i = 0; i < topics.length; i++) {
        var t = topics[i];
        pushLog(t.n, t.v);
        var row = topicsBody.querySelector('.topic-row[data-topic="' + t.n + '"]');
        if (!row) continue;
        var live = t.hz > 0.05;
        var rate = row.querySelector('.rate');
        rate.textContent = live ? t.hz.toFixed(1) + ' Hz' : 'idle';
        rate.className = 'rate ' + (live ? 'hz-live' : 'muted');
        row.querySelector('.latest').textContent = t.v || '-';
        if (topicExpanded[t.n]) {
            var detail = topicsBody.querySelector('.topic-detail[data-topic="' + t.n + '"]');
            var pre = detail.querySelector('.stream');
            var log = topicLog[t.n] || [];
            var lines = '';
            for (var j = log.length - 1; j >= 0; j--) {
                lines += log[j].ts + '   ' + log[j].v + '\n';
            }
            pre.textContent = lines || '(no messages yet)';
        }
    }
}

topicsBody.addEventListener('click', function (e) {
    var row = e.target.closest('.topic-row');
    if (!row) return;
    var name = row.getAttribute('data-topic');
    topicExpanded[name] = !topicExpanded[name];
    row.classList.toggle('expanded', topicExpanded[name]);
    var caret = row.querySelector('.caret');
    if (caret) caret.textContent = topicExpanded[name] ? '▾' : '▸';
    var detail = topicsBody.querySelector('.topic-detail[data-topic="' + name + '"]');
    if (detail) detail.classList.toggle('open', topicExpanded[name]);
});

function updateStatus(d) {
    if (d.msg) {
        banner.textContent = d.msg;
        banner.classList.toggle('picking', !!d.arm && d.arm.indexOf('PICK') === 0);
    }
    if (d.topics) renderTopics(d.topics);
    renderRecording(d.recording);
    for (var c in chips) {
        chips[c].classList.toggle('looking', d.zone === c);
        chips[c].classList.toggle('selected', d.selected === c);
    }
    gestureStat.textContent = 'gesture: ' + (d.gesture || '—');
    gestureStat.classList.toggle('active', d.gesture === 'HAND_RAISED');
    armStat.textContent = 'arm: ' + (d.arm || '—');
    armStat.classList.toggle('active', !!d.arm && d.arm !== 'IDLE');
    connStat.textContent = d.head_pose_fresh
        ? ('head yaw ' + d.yaw_deg + '°')
        : 'no face detected';
}

function connect() {
    ws = new WebSocket(wsProto + location.host + '/ws');

    ws.onopen = function () {
        reconnectDelay = 500;   // reset backoff on a good connection
        connStat.textContent = 'connected';
        sendFrame();
    };

    ws.onclose = function () {
        // reconnect with capped exponential backoff so a container restart or
        // network blip recovers on its own instead of wedging every browser
        connStat.textContent = 'disconnected — reconnecting…';
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    };

    ws.onerror = function () {
        try { ws.close(); } catch (x) { }   // force onclose -> reconnect path
    };

    ws.onmessage = function (e) {
        try {
            var m = JSON.parse(e.data);
            if (m.t === "head_pose") {
                // head_pose replies pace the webcam upload loop: send the next
                // frame as soon as perception finished with the previous one
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
}

connect();
