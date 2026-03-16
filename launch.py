from pathlib import Path
from flask import Flask, request, render_template_string, jsonify, Response
from flask_socketio import SocketIO, emit
import subprocess
import sys
import socket
import time
import re
import signal
import shutil
import threading
import logging
import os
import importlib.util

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

def escape_html(s):
    """Escape only &, <, > for safe innerHTML – leaves quotes and apostrophes untouched."""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# --- CONFIGURATION ---
VALID_USERNAME_REGEX = re.compile(r'^[a-zA-Z0-9_]{3,16}$')
FORBIDDEN_LIST = ["CubeUniform840", "Admin", "Owner"]
PASS_KEY = "1234"
SERVER_IP = "77.103.184.72"
# SERVER_IP = "eu.chickencraft.nl"
JVM_OPTS = "-Xmx3G -Xms3G -XX:+UnlockExperimentalVMOptions -XX:+UseG1GC -XX:G1NewSizePercent=20 -XX:G1ReservePercent=20 -XX:MaxGCPauseMillis=50 -XX:G1HeapRegionSize=32M -XX:+AlwaysPreTouch -XX:+ParallelRefProcEnabled -XX:+DisableExplicitGC"

# Thread-safe process tracking
active_processes = {}
processes_lock = threading.Lock()

# Track connected clients
connected_clients = 0
clients_lock = threading.Lock()
logging.basicConfig(level=logging.INFO)

# --- HTML TEMPLATE (with 4-space indented JavaScript) ---
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Minecraft - Java Edition</title>
    <style>
        * {
            box-sizing: border-box;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        html, body {
            height: 100%;
            margin: 0;
            padding: 0;
            width: 100%;
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background-color: #0a0a0a;
            overflow: hidden;
        }

        body.show-bg {
            background: radial-gradient(circle at 20% 30%, rgba(20, 40, 80, 0.4) 0%, rgba(0, 0, 0, 0.8) 100%),
                        url('/static/bg.png') no-repeat center center fixed;
            background-size: cover;
        }

        .blur-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            backdrop-filter: blur(8px) saturate(180%);
            -webkit-backdrop-filter: blur(8px) saturate(180%);
            z-index: 0;
            pointer-events: none;
            display: none;
            background: rgba(0, 0, 0, 0.2);
        }
        body.show-bg .blur-overlay {
            display: block;
        }

        .main-container {
            position: relative;
            z-index: 10;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            width: 100%;
            margin-top: -5vh;
            animation: appEntry 1s ease-out;
        }

        @keyframes appEntry {
            0% { opacity: 0; transform: scale(1.05); filter: blur(10px); }
            100% { opacity: 1; transform: scale(1); filter: blur(0); }
        }

        .top-logo {
            width: 550px;
            max-width: 90%;
            margin-bottom: 40px;
            filter: drop-shadow(0 20px 40px rgba(0, 0, 0, 0.8));
        }

        @keyframes float {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-10px); }
        }

        .glass-card {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 24px;
            background: rgba(15, 15, 20, 0.6);
            backdrop-filter: blur(20px) saturate(180%);
            -webkit-backdrop-filter: blur(20px) saturate(180%);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 40px;
            width: 440px;
            padding: 48px 52px;
            text-align: center;
            color: white;
            animation: float 6s ease-in-out infinite;
            box-shadow: 0 40px 80px rgba(0, 0, 0, 0.8),
                        inset 0 0 30px rgba(255, 255, 255, 0.02);
        }

        form {
            display: flex;
            flex-direction: column;
            width: 100%;
        }

        #forbidden-warn {
            color: #ff8a8a;  /* Brighter red */
            font-size: 9px;
            margin: 12px 0 6px 6px;
            font-weight: 700;
            text-align: left;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            animation: pulse-red 1.8s infinite;
            text-shadow: 0 0 12px rgba(255, 138, 138, 0.5);
        }

        @keyframes pulse-red {
            0%, 100% { opacity: 1; text-shadow: 0 0 12px #ff8a8a; }
            50% { opacity: 0.7; text-shadow: 0 0 20px #ff5a5a; }
        }

        .status-container {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 14px;
            margin-bottom: 24px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
            transition: box-shadow 0.3s ease;
        }
        .status-dot.online {
            background: #2ecc71;
            box-shadow: 0 0 15px #2ecc71, 0 0 30px rgba(46, 204, 113, 0.3);
        }
        .status-dot.offline {
            background: #e74c3c;
            box-shadow: 0 0 15px #e74c3c, 0 0 30px rgba(231, 76, 60, 0.3);
        }

        .subtitle {
            font-size: 10px;
            letter-spacing: 4px;
            text-transform: uppercase;
            color: rgba(255, 255, 255, 0.4);
            font-weight: 600;
        }

        .input-wrapper {
            position: relative;
            width: 100%;
            margin-bottom: 12px;
        }

        input {
            width: 100%;
            padding: 18px 22px;
            background: rgba(255, 255, 255, 0.03) !important;
            border: 1px solid rgba(255, 255, 255, 0.08) !important;
            border-radius: 24px;
            color: white !important;
            font-size: 15px;
            caret-color: #6ab0ff !important; /* Brighter blue */
            outline: none !important;
            box-shadow: none !important;
            backdrop-filter: blur(10px) saturate(150%);
            -webkit-backdrop-filter: blur(10px) saturate(150%);
            appearance: none;
            -webkit-appearance: none;
            transition: all 0.3s ease;
        }

        input:focus {
            border-color: rgba(106, 176, 255, 0.5) !important;
            background: rgba(255, 255, 255, 0.05) !important;
            box-shadow: 0 0 0 4px rgba(106, 176, 255, 0.1) !important;
        }

        input:-webkit-autofill {
            -webkit-text-fill-color: white !important;
            -webkit-box-shadow: 0 0 0px 1000px rgba(20, 20, 30, 0.9) inset !important;
            transition: background-color 5000s ease-in-out 0s;
        }

        .toggle-pass {
            position: absolute;
            right: 20px;
            top: 50%;
            transform: translateY(-50%);
            cursor: pointer;
            display: flex;
            align-items: center;
            z-index: 10;
            padding: 8px;
        }

        .toggle-pass svg {
            width: 20px;
            height: 20px;
            fill: rgba(255, 255, 255, 0.5) !important;
            transition: all 0.2s ease;
        }

        .toggle-pass:hover svg {
            fill: white !important;
            filter: drop-shadow(0 0 8px #6ab0ff);
        }

        button {
            width: 100%;
            background: rgba(106, 176, 255, 0.2);
            color: white;
            border: 1px solid rgba(106, 176, 255, 0.3);
            padding: 18px 24px;
            border-radius: 28px;
            font-weight: 600;
            font-size: 14px;
            letter-spacing: 1.5px;
            cursor: pointer;
            margin-top: 20px;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            transition: all 0.3s ease;
            text-transform: uppercase;
        }

        button:hover:not(:disabled) {
            background: rgba(106, 176, 255, 0.3);
            border-color: rgba(106, 176, 255, 0.6);
            transform: translateY(-2px);
            box-shadow: 0 20px 30px rgba(106, 176, 255, 0.2);
        }

        button:disabled {
            background: rgba(200, 70, 70, 0.15) !important;
            border: 1px solid rgba(255, 120, 120, 0.2) !important;
            color: rgba(255, 200, 200, 0.6) !important;
            text-shadow: 0 0 10px rgba(255, 100, 100, 0.4);
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }

        /* --- LIVE CONSOLE --- */
        #console-container {
            display: none;
            width: 85%;
            max-width: 1200px;
            height: 65%;
            background: rgba(8, 8, 12, 0.85);
            backdrop-filter: blur(20px) saturate(180%);
            -webkit-backdrop-filter: blur(20px) saturate(180%);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 32px;
            padding: 28px 32px;
            overflow-y: auto;
            text-align: left;
            font-family: 'Fira Code', 'Consolas', monospace;
            color: #ffffff;
            box-shadow: 0 50px 100px rgba(0, 0, 0, 0.9);
            scrollbar-width: thin;
            scrollbar-color: rgba(255, 255, 255, 0.15) transparent;
            transition: all 0.3s ease;
        }

        #console-container:hover {
            border-color: rgba(255, 255, 255, 0.15);
            box-shadow: 0 70px 140px rgba(0, 0, 0, 1);
        }

        #console-container::-webkit-scrollbar {
            width: 6px;
        }

        #console-container::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            transition: background 0.3s;
        }

        #console-container::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.25);
        }

        #flask-status {
            font-size: 8px;
            margin-top: -10px;
            margin-bottom: 10px;
            color: rgba(255, 255, 255, 0.3);
            letter-spacing: 1px;
        }
        #flask-status.online {
            color: rgba(46, 204, 113, 0.7);
        }
        #flask-status.offline {
            color: rgba(231, 76, 60, 0.7);
        }

        .log-group {
            border-left: 2px solid rgba(255, 255, 255, 0.2);
            margin-bottom: 6px;
            padding-left: 2px;
            background: none;
        }

        .log-line {
            font-size: 13px;
            padding: 2px 0 2px 16px;
            line-height: 1.5;
            background: none;
            margin: 0;
            font-family: inherit;
            white-space: pre-wrap;
            word-break: break-word;
        }

        .log-line.continuation-line {
            padding-left: 18px;
            opacity: 0.9;
        }

        /* --- AMBIENT MODE WIDGET --- */
        .controls {
            position: fixed;
            bottom: 32px;
            left: 32px;
            display: flex;
            align-items: center;
            gap: 16px;
            z-index: 100;
            background: rgba(10, 10, 15, 0.4);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            padding: 10px 18px;
            border-radius: 36px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            box-shadow: 0 15px 30px rgba(0, 0, 0, 0.5);
        }

        .switch {
            position: relative;
            width: 40px;
            height: 20px;
        }
        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: rgba(255, 255, 255, 0.1);
            transition: 0.3s;
            border-radius: 34px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 16px;
            width: 16px;
            left: 2px;
            bottom: 1px;
            background-color: rgba(255, 255, 255, 0.7);
            transition: 0.3s;
            border-radius: 50%;
        }
        input:checked + .slider {
            background-color: rgba(106, 176, 255, 0.3);
            border-color: rgba(106, 176, 255, 0.4);
        }
        input:checked + .slider:before {
            transform: translateX(20px);
            background-color: #6ab0ff;
            box-shadow: 0 0 10px #6ab0ff;
        }

        .control-label {
            font-size: 10px;
            color: rgba(255, 255, 255, 0.5);
            text-transform: uppercase;
            letter-spacing: 2px;
            font-weight: 500;
        }

        .watermark {
            position: fixed;
            bottom: 32px;
            right: 32px;
            font-size: 10px;
            color: rgba(255, 255, 255, 0.2);
            font-weight: 400;
            letter-spacing: 3px;
            pointer-events: none;
            z-index: 100;
        }
    </style>
</head>
<body id="bodyNode">
    <div class="blur-overlay"></div>

    <div class="controls">
        <label class="switch">
            <input type="checkbox" id="bgToggle">
            <span class="slider"></span>
        </label>
        <span class="control-label">Ambient Mode</span>
    </div>

    <div class="main-container" id="main-ui">
        <img src="/static/logo.svg" class="top-logo">
        <div class="glass-card" id="login-card">
            <div class="status-container">
                <div id="statusDot" class="status-dot"></div>
                <div class="subtitle">Minecraft Java 1.21.11</div>
            </div>
            <div id="flask-status" style="font-size: 8px; margin-top: -10px; margin-bottom: 10px; color: rgba(255,255,255,0.3);">
                Flask: <span id="flask-status-text">Connected</span>
            </div>
            {% if error %}<div style="color:#ff5555; font-size:11px; margin-bottom:20px; font-weight:800; text-transform:uppercase;">{{ error }}</div>{% endif %}
            
            <form id="launchForm">
                <div class="input-wrapper">
                    <input type="text" id="username" name="username" placeholder="Username" autocomplete="off">
                    <div id="forbidden-warn" style="display:none; color:#ff5555; font-size:9px; margin-top:8px; font-weight:800; text-align:left;">[!] RESTRICTED IDENTITY DETECTED</div>
                </div>

                <div id="pass-container" class="input-wrapper" style="display:none;">
                    <input type="password" id="password" name="password" placeholder="SECURE_KEY">
                    <div class="toggle-pass">
                        <svg id="eyeIcon" viewBox="0 0 24 24"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z" /></svg>
                    </div>
                </div>

                <button type="submit" id="launchBtn">INITIALIZE CORE</button>
            </form>
        </div>

        <div id="console-container">
            <div style="border-bottom:1px solid rgba(0,255,136,0.2); padding-bottom:15px; margin-bottom:20px; font-size:10px; letter-spacing:3px;">SYSTEM_CORE // LIVE_LOGS</div>
            <div id="log-output"></div>
        </div>
    </div>

    <div class="watermark">PORTABLE_MC // APEX_V6.5</div>
    <script>
        (function loadAnsiUp() {
            var script = document.createElement('script');
            script.src = '{{ url_for("static", filename="ansi_up.min.js") }}';
            script.onload = function() {
                console.log('✅ ansi_up loaded from local file');
            };
            script.onerror = function() {
                console.warn('❌ Local ansi_up failed, loading from cdnjs...');
                var fallback = document.createElement('script');
                fallback.src = 'https://cdn.jsdelivr.net/npm/ansi_up@5.2.1/ansi_up.min.js';
                fallback.onload = function() {
                    console.log('✅ ansi_up loaded from cdnjs');
                };
                fallback.onerror = function() {
                    console.error('❌ Both local and cdnjs ansi_up failed.');
                };
                document.head.appendChild(fallback);
            };
            document.head.appendChild(script);
        })();
    </script>
    <!-- Main application code – defines initApp -->
    <script>
        function initApp() {
            const forbidden = {{ forbidden_list | tojson }};
            const consoleNode = document.getElementById('console-container');
            const loginCard = document.getElementById('login-card');
            const launchBtn = document.getElementById('launchBtn');
            const logOutput = document.getElementById('log-output');
            const userField = document.getElementById('username');
            const passField = document.getElementById('password');
            const passContainer = document.getElementById('pass-container');
            const warnText = document.getElementById('forbidden-warn');
            const bgToggle = document.getElementById('bgToggle');
            const statusDot = document.getElementById('statusDot');
            const flaskStatusText = document.getElementById('flask-status-text');

            const socket = io({
                reconnection: true,
                reconnectionAttempts: Infinity,
                reconnectionDelay: 1000,
                reconnectionDelayMax: 5000,
                timeout: 2000
            });

            let currentEventSource = null;
            let fadeTimer = null;
            let currentGroup = null;
            let lastLineColor = '#ffffff';
            let lastCheckedUser = '';
            let minecraftStatusInterval = null;
            let shouldReloadOnReconnect = false;
            let forbiddenInterval = null;

            // Create ANSI converter instance using ansi_up
            let ansiConverter = null;
            if (typeof AnsiUp !== 'undefined') {
                ansiConverter = new AnsiUp();
            } else {
                console.warn('AnsiUp not loaded; logs will be raw.');
            }

            // --- WebSocket event handlers ---
            socket.on('connect', function() {
                console.log('WebSocket connected');
                if (shouldReloadOnReconnect) {
                    if (!sessionStorage.getItem('reloaded')) {
                        sessionStorage.setItem('reloaded', 'true');
                        console.log('Server reconnected – reloading page for updates');
                        window.location.reload();
                        return;
                    } else {
                        console.log('Server reconnected – reload already performed this session, continuing without reload');
                    }
                }
                launchBtn.disabled = false;
                launchBtn.innerText = "INITIALIZE CORE";
                flaskStatusText.innerText = 'Connected';
                flaskStatusText.style.color = '#2ecc71';
                socket.emit('ping_minecraft');
                if (minecraftStatusInterval) clearInterval(minecraftStatusInterval);
                minecraftStatusInterval = setInterval(() => {
                    if (socket.connected) socket.emit('ping_minecraft');
                }, 10000);
            });

            socket.on('disconnect', function(reason) {
                console.log('WebSocket disconnected:', reason);
                launchBtn.disabled = true;
                launchBtn.innerText = "CORE OFFLINE";
                flaskStatusText.innerText = 'Disconnected';
                flaskStatusText.style.color = '#e74c3c';
                statusDot.className = 'status-dot offline';
                if (minecraftStatusInterval) {
                    clearInterval(minecraftStatusInterval);
                    minecraftStatusInterval = null;
                }
                shouldReloadOnReconnect = true;
            });

            socket.on('minecraft_status', function(data) {
                statusDot.className = data.online ? 'status-dot online' : 'status-dot offline';
            });

            // --- Core UI functions ---
            function appendLog(rawAnsiText) {
                const line = document.createElement('div');
                line.className = 'log-line';

                let htmlContent = rawAnsiText;
                if (ansiConverter) {
                    htmlContent = ansiConverter.ansi_to_html(rawAnsiText);
                }
                line.innerHTML = htmlContent;

                logOutput.appendChild(line);
                consoleNode.scrollTop = consoleNode.scrollHeight;
                triggerScrollFade();
            }

            function triggerScrollFade() {
                consoleNode.classList.add('active-scroll');
                if (fadeTimer) clearTimeout(fadeTimer);
                fadeTimer = setTimeout(() => consoleNode.classList.remove('active-scroll'), 1500);
            }

            // --- UI event handlers ---
            function showPass() {
                passField.type = "text";
                document.getElementById('eyeIcon').style.fill = "#2563eb";
            }

            function hidePass() {
                passField.type = "password";
                document.getElementById('eyeIcon').style.fill = "rgba(255, 255, 255, 0.6)";
            }

            function checkForbidden() {
                const currentUser = userField.value.trim().toLowerCase();
                if (currentUser === lastCheckedUser) return;
                lastCheckedUser = currentUser;
                console.log(`[checkForbidden] username: "${currentUser}"`);
                const isMatch = forbidden.some(name => name.toLowerCase() === currentUser);

                if (isMatch) {
                    passContainer.style.display = "block";
                    warnText.style.display = "block";
                    passField.required = true;
                    void passContainer.offsetHeight;
                    void warnText.offsetHeight;
                } else {
                    passContainer.style.display = "none";
                    warnText.style.display = "none";
                    passField.required = false;
                    void passContainer.offsetHeight;
                    void warnText.offsetHeight;
                }
            }

            async function startLaunch() {
                if (currentEventSource) {
                    currentEventSource.close();
                    currentEventSource = null;
                }
                const user = userField.value;
                const pass = passField.value;
                localStorage.setItem('last_mc_user', user);

                launchBtn.disabled = true;
                launchBtn.innerText = "INITIALIZING...";
                loginCard.style.display = "none";
                consoleNode.style.display = "block";
                logOutput.innerHTML = "";
                currentGroup = null;
                lastLineColor = '#ffffff';

                const eventSource = new EventSource(`/stream?username=${encodeURIComponent(user)}&password=${encodeURIComponent(pass)}`);
                currentEventSource = eventSource;

                eventSource.onmessage = function(e) {
                    if (e.data === "CLOSE") {
                        setTimeout(() => {
                            eventSource.close();
                            currentEventSource = null;
                            consoleNode.style.cursor = "pointer";
                            launchBtn.disabled = false;
                            launchBtn.innerText = "INITIALIZE CORE";
                        }, 500);
                        return;
                    }
                    appendLog(e.data);
                };

                eventSource.onerror = function() {
                    eventSource.close();
                    currentEventSource = null;
                    appendLog('[SYSTEM] CONNECTION LOST');
                    launchBtn.disabled = false;
                    launchBtn.innerText = "INITIALIZE CORE";
                };
            }

            function toggleBackground() {
                const active = bgToggle.checked;
                document.getElementById('bodyNode').classList.toggle('show-bg', active);
                localStorage.setItem('ambient_mode', active ? 'on' : 'off');
            }

            // --- Set up event listeners ---
            document.getElementById('launchForm').addEventListener('submit', (e) => {
                e.preventDefault();
                startLaunch();
            });

            userField.addEventListener('input', checkForbidden);
            userField.addEventListener('change', checkForbidden);
            userField.addEventListener('paste', () => setTimeout(checkForbidden, 10));

            const togglePass = document.querySelector('.toggle-pass');
            togglePass.addEventListener('mousedown', showPass);
            togglePass.addEventListener('mouseup', hidePass);
            togglePass.addEventListener('mouseleave', hidePass);

            bgToggle.addEventListener('change', toggleBackground);

            consoleNode.addEventListener('click', function() {
                loginCard.style.display = "block";
                consoleNode.style.display = "none";
                if (socket.connected) {
                    launchBtn.disabled = false;
                    launchBtn.innerText = "INITIALIZE CORE";
                } else {
                    launchBtn.disabled = true;
                    launchBtn.innerText = "CORE OFFLINE";
                }
            });

            consoleNode.addEventListener('scroll', triggerScrollFade);
            consoleNode.addEventListener('mousemove', triggerScrollFade);

            const savedUser = localStorage.getItem('last_mc_user');
            if (savedUser) userField.value = savedUser;
            const savedMode = localStorage.getItem('ambient_mode');
            if (savedMode === 'on' || savedMode === null) {
                bgToggle.checked = true;
                document.getElementById('bodyNode').classList.add('show-bg');
            }

            forbiddenInterval = setInterval(checkForbidden, 500);
            window.addEventListener('beforeunload', function() {
                clearInterval(forbiddenInterval);
            });

            window.addEventListener('focus', checkForbidden);
            checkForbidden();
        }
    </script>
    <!-- Socket.IO version dynamic fallback loader -->
    <script>
        (function loadSocketIO() {
            const versions = [
                '4.8.3', '4.7.2', '4.6.2', '4.5.4', '4.4.4',
                '4.3.5', '4.2.2', '4.1.2', '4.0.1'
            ];
            let currentIndex = -1;

            function tryNext() {
                if (currentIndex === -1) {
                    console.log('Attempting to load local socket.io.js...');
                    loadScript('{{ url_for("static", filename="socket.io.js") }}');
                } else if (currentIndex < versions.length) {
                    const ver = versions[currentIndex];
                    console.log(`Attempting CDN version ${ver}...`);
                    loadScript(`https://cdn.socket.io/${ver}/socket.io.min.js`);
                } else {
                    console.error('All Socket.IO sources failed.');
                    const markSocketIoFailure = function() {
                        const btn = document.getElementById('launchBtn');
                        if (btn) {
                            btn.disabled = true;
                            btn.innerText = 'SOCKET.IO LOAD FAILED';
                        }
                    };
                    if (document.readyState === 'loading') {
                        document.addEventListener('DOMContentLoaded', markSocketIoFailure);
                    } else {
                        setTimeout(markSocketIoFailure, 0);
                    }
                    return;
                }
                currentIndex++;
            }

            function loadScript(src) {
                const script = document.createElement('script');
                script.src = src;
                script.onload = function() {
                    console.log(`✅ Successfully loaded: ${src}`);
                    initApp(); // initApp is defined and ansi_up already loaded
                };
                script.onerror = function() {
                    console.warn(`❌ Failed to load: ${src}`);
                    tryNext();
                };
                document.head.appendChild(script);
            }

            tryNext();
        })();
    </script>
</body>
</html>
"""

# --- ROUTES ---

@socketio.on('connect')
def handle_connect():
    global connected_clients
    with clients_lock:
        connected_clients += 1
        client_id = request.sid
        print(f'Client connected: {client_id} (Total: {connected_clients})')
    
    # Send initial status
    emit('status', {'core': 'online', 'minecraft': 'checking'})

@socketio.on('disconnect')
def handle_disconnect():
    global connected_clients
    with clients_lock:
        connected_clients -= 1
        client_id = request.sid
        print(f'Client disconnected: {client_id} (Total: {connected_clients})')

@socketio.on('ping_minecraft')
def handle_ping_minecraft():
    """Check Minecraft server status on demand"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect((SERVER_IP, 25565))
        s.close()
        emit('minecraft_status', {'online': True})
    except Exception:
        emit('minecraft_status', {'online': False})

@app.route("/ping")
def ping():
    """Checks if the Minecraft server is online."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect((SERVER_IP, 25565))
        s.close()
        return jsonify(online=True)
    except Exception:
        # Always return 200 OK with online=false
        return jsonify(online=False), 200

@app.route("/stream")
def stream():
    # --- PORTABLEMC AVAILABILITY CHECK ---
    def is_portablemc_available():
        if shutil.which("portablemc"):
            return True
        return importlib.util.find_spec("portablemc") is not None

    if not is_portablemc_available():
        def error_gen():
            yield "data: \x1b[91m[!] PORTABLEMC NOT FOUND\x1b[0m\n\n"
            yield "data: \x1b[93mPlease install it via 'pip install portablemc'.\x1b[0m\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    # --- GET USER INPUT ---
    user = request.args.get("username", "Player").strip()
    password = request.args.get("password", "")

    # --- VALIDATIONS (plain text, escaped) ---
    if not user:
        def error_gen():
            msg = "\x1b[91m[!] USERNAME REQUIRED\x1b[0m"
            # msg is ANSI, send raw
            yield f"data: {msg}\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    if not VALID_USERNAME_REGEX.match(user):
        def error_gen():
            msg1 = "\x1b[91m[!] INVALID USERNAME\x1b[0m"
            msg2 = "\x1b[93mUsername must be 3-16 characters and only letters, numbers, or underscore.\x1b[0m"
            yield f"data: {msg1}\n\n"
            yield f"data: {msg2}\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    user_lower = user.lower()
    forbidden_lower = [name.lower() for name in FORBIDDEN_LIST]
    if user_lower in forbidden_lower and password != PASS_KEY:
        def error_gen():
            msg = "\x1b[91m[!] ACCESS DENIED – INVALID SECURE_KEY\x1b[0m"
            yield f"data: {msg}\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    # --- PREVENT MULTIPLE LAUNCHES (thread-safe) ---
    with processes_lock:
        if user in active_processes and active_processes[user].poll() is None:
            def error_gen():
                lines = [
                    "\x1b[91m[!] CORE BUSY\x1b[0m",
                    "\x1b[93mAnother Minecraft instance is already running.\x1b[0m",
                    "\x1b[90mPlease close the game before launching again.\x1b[0m"
                ]
                for line in lines:
                    yield f"data: {line}\n\n"
                yield "data: CLOSE\n\n"
            return Response(error_gen(), mimetype="text/event-stream")
        if user in active_processes:
            del active_processes[user]

    # --- BUILD COMMAND ---
    if shutil.which("portablemc"):
        launcher_cmd = ["portablemc"]
    else:
        launcher_cmd = [sys.executable, "-m", "portablemc"]

    global_args = [
        "--main-dir", ".",
        "--timeout", "60",
        "--output", "human-color"
    ]
    start_args = [
        "--server", SERVER_IP,
        "--jvm-args", JVM_OPTS,
        "fabric:",
        "-u", user
    ]

    # Custom Java path
    java_exe = "java.exe" if os.name == "nt" else "java"
    java_bin = Path.cwd() / "jvm" / "java-runtime-delta" / "bin" / java_exe
    if java_bin.exists():
        start_args.insert(0, "--jvm")
        start_args.insert(1, str(java_bin))

    cmd = launcher_cmd + global_args + ["start"] + start_args

    # --- DISCONNECT DETECTION ---
    closed_event = threading.Event()
    progress_re = re.compile(r"(\d+/\d+)")

    def generate():
        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            with processes_lock:
                active_processes[user] = process
                logging.info(f"Started process for {user} with PID {process.pid}")

            last_progress = ""
            last_send_time = time.perf_counter()
            update_interval = 0.15
            ok_reached = False

            while True:
                if closed_event.is_set():
                    disc_msg = "\x1b[91m[SYSTEM] CONNECTION CLOSED\x1b[0m"
                    try:
                        yield f"data: {disc_msg}\n\n"
                    except (BrokenPipeError, OSError):
                        pass
                    break

                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue

                raw_line = line.rstrip('\n')
                now = time.perf_counter()

                if not ok_reached and "[ OK ]" in raw_line:
                    ok_reached = True

                progress_match = progress_re.search(raw_line)

                if progress_match and not ok_reached:
                    current_file = progress_match.group(1)
                    if current_file != last_progress and (now - last_send_time) > update_interval:
                        try:
                            yield f"data: {raw_line}\n\n"
                        except (BrokenPipeError, OSError):
                            break
                        last_progress = current_file
                        last_send_time = now
                else:
                    try:
                        yield f"data: {raw_line}\n\n"
                    except (BrokenPipeError, OSError):
                        break

        except FileNotFoundError as e:
            if not closed_event.is_set():
                try:
                    yield f"data: \x1b[91m[SYSTEM] Launcher not found: {str(e)}\x1b[0m\n\n"
                except Exception:
                    pass
        except Exception as e:
            if not closed_event.is_set():
                try:
                    yield f"data: [SYSTEM ERROR] {str(e)}\n\n"
                except Exception:
                    pass
        finally:
            if process:
                process.stdout.close()
                kill_process_tree(process)
            with processes_lock:
                if user in active_processes:
                    del active_processes[user]
                    logging.info(f"Removed process entry for {user}")

            # Session messages – raw ANSI
            try:
                ended_msg = "\x1b[90m[SYSTEM] SESSION ENDED\x1b[0m"
                tip_msg = "\x1b[34m[TIP] Click the console to return to login.\x1b[0m"
                yield f"data: {ended_msg}\n\n"
                yield f"data: {tip_msg}\n\n"
                yield "data: CLOSE\n\n"
            except GeneratorExit:
                pass
            except Exception:
                pass

    response = Response(generate(), mimetype="text/event-stream")
    response.call_on_close(closed_event.set)
    return response

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE, forbidden_list=FORBIDDEN_LIST)

def kill_minecraft_java_processes():
    """Find and kill Java processes that look like Minecraft clients (by command line)."""
    logging.info("Searching for Minecraft Java processes...")
    try:
        # PowerShell: get PIDs of Java processes whose command line contains our JVM path or keywords
        ps_command = """
        Get-Process | Where-Object { $_.ProcessName -match 'java' } | ForEach-Object {
            $p = $_
            $cmd = (Get-WmiObject Win32_Process -Filter "ProcessId = $($p.Id)").CommandLine
            if ($cmd -match 'java-runtime-delta' -or $cmd -match 'fabric' -or $cmd -match 'minecraft') {
                Write-Output $p.Id
            }
        }
        """
        result = subprocess.run(
            ['powershell', '-Command', ps_command],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5
        )
        if result.returncode == 0:
            pids_found = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids_found.add(int(line))
            for pid in pids_found:
                logging.info(f"Found candidate Minecraft Java process: PID {pid}")
                kill_result = subprocess.run(
                    ['taskkill', '/F', '/PID', str(pid)],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if kill_result.returncode == 0:
                    logging.info(f"Killed Java PID {pid}")
                else:
                    # Process may have died between discovery and kill attempt
                    logging.debug(f"Could not kill Java PID {pid} (already terminated)")
        else:
            logging.error(f"PowerShell query failed: {result.stderr}")
    except Exception as e:
        logging.error(f"Error in kill_minecraft_java_processes: {e}")

def kill_process_tree(proc):
    """Kill a process and all its children using taskkill."""
    if proc.poll() is not None:
        logging.debug(f"Process {proc.pid} already dead")
        return
    try:
        # First try graceful termination
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        logging.error(f"Error terminating process {proc.pid}: {e}")
    # Force kill the entire tree
    subprocess.run(
        ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    logging.info(f"Force‑killed process tree with PID {proc.pid}")

def cleanup_processes():
    """Terminate any remaining Minecraft Java processes (launcher cleanup is optional)."""
    logging.info("Cleaning up Minecraft Java processes...")
    kill_minecraft_java_processes()
    # The launcher process (portablemc) is already dead or will be reaped automatically
    # No need to track or kill it separately

def graceful_shutdown(sig, frame):
    logging.info("SHUTTING DOWN CORE...")
    cleanup_processes()
    sys.exit(0)

# Set signal handler for SIGINT (Ctrl+C)
signal.signal(signal.SIGINT, graceful_shutdown)

if __name__ == "__main__":
    try:
        socketio.run(app, port=5000, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        # Fallback if signal handler doesn't catch it
        graceful_shutdown(None, None)
