from pathlib import Path
from ansi2html import Ansi2HTMLConverter
from flask import Flask, request, render_template_string, jsonify, Response
import subprocess
import sys
import socket
import time
import re
import signal
import shutil
import threading
import html
import logging
import os
import importlib.util

app = Flask(__name__)

def escape_html(s):
    """Escape only &, <, > for safe innerHTML – leaves quotes and apostrophes untouched."""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# --- CONFIGURATION ---
VALID_USERNAME_REGEX = re.compile(r'^[a-zA-Z0-9_]{3,16}$')
FORBIDDEN_LIST = ["CubeUniform840", "Admin", "Owner"] # Add forbidden usernames
PASS_KEY = "1234" # Add forbidden username password
SERVER_IP = "eu.chickencraft.nl" # Add server IP for quick join or disable it with ""
JVM_OPTS = "-Xmx3G -Xms3G -XX:+UnlockExperimentalVMOptions -XX:+UseG1GC -XX:G1NewSizePercent=20 -XX:G1ReservePercent=20 -XX:MaxGCPauseMillis=50 -XX:G1HeapRegionSize=32M -XX:+AlwaysPreTouch -XX:+ParallelRefProcEnabled -XX:+DisableExplicitGC"

# Thread-safe process tracking
active_processes = {}
processes_lock = threading.Lock()

# ANSI to HTML converter (dark background, inline styles)
ansi_converter = Ansi2HTMLConverter(dark_bg=True, inline=True)
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
            color: #ff5a5a;
            font-size: 9px;
            margin: 12px 0 6px 6px;
            font-weight: 700;
            text-align: left;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            animation: pulse-red 1.8s infinite;
            text-shadow: 0 0 8px rgba(255, 90, 90, 0.3);
        }

        @keyframes pulse-red {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
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
            box-shadow: 0 0 15px #2ecc71;
        }
        .status-dot.offline {
            background: #e74c3c;
            box-shadow: 0 0 15px #e74c3c;
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
            caret-color: #4d9eff !important;
            outline: none !important;
            box-shadow: none !important;
            backdrop-filter: blur(10px) saturate(150%);
            -webkit-backdrop-filter: blur(10px) saturate(150%);
            appearance: none;
            -webkit-appearance: none;
            transition: all 0.3s ease;
        }

        input:focus {
            border-color: rgba(77, 158, 255, 0.5) !important;
            background: rgba(255, 255, 255, 0.05) !important;
            box-shadow: 0 0 0 4px rgba(77, 158, 255, 0.1) !important;
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
            filter: drop-shadow(0 0 8px #4d9eff);
        }

        button {
            width: 100%;
            background: rgba(77, 158, 255, 0.2);
            color: white;
            border: 1px solid rgba(77, 158, 255, 0.3);
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
            background: rgba(77, 158, 255, 0.3);
            border-color: rgba(77, 158, 255, 0.6);
            transform: translateY(-2px);
            box-shadow: 0 20px 30px rgba(77, 158, 255, 0.2);
        }

        button:disabled {
            background: rgba(180, 70, 70, 0.15) !important;
            border: 1px solid rgba(255, 100, 100, 0.2) !important;
            color: rgba(255, 160, 160, 0.6) !important;
            text-shadow: 0 0 10px rgba(255, 0, 0, 0.2);
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
            background-color: rgba(77, 158, 255, 0.3);
            border-color: rgba(77, 158, 255, 0.4);
        }
        input:checked + .slider:before {
            transform: translateX(20px);
            background-color: #4d9eff;
            box-shadow: 0 0 10px #4d9eff;
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
            <input type="checkbox" id="bgToggle" onchange="toggleBackground()">
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
            
            {% if error %}<div style="color:#ff5555; font-size:11px; margin-bottom:20px; font-weight:800; text-transform:uppercase;">{{ error }}</div>{% endif %}
            
            <form id="launchForm" onsubmit="event.preventDefault(); startLaunch();">
                <div class="input-wrapper">
                    <input type="text" id="username" name="username" placeholder="Username" autofocus oninput="checkForbidden()" autocomplete="off">
                    <div id="forbidden-warn" style="display:none; color:#ff5555; font-size:9px; margin-top:8px; font-weight:800; text-align:left;">[!] RESTRICTED IDENTITY DETECTED</div>
                </div>

                <div id="pass-container" class="input-wrapper" style="display:none;">
                    <input type="password" id="password" name="password" placeholder="SECURE_KEY">
                    <div class="toggle-pass" onmousedown="showPass()" onmouseup="hidePass()" onmouseleave="hidePass()">
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
        const forbidden = {{ forbidden_list | tojson }};
        const consoleNode = document.getElementById('console-container');
        const loginCard = document.getElementById('login-card');
        const launchBtn = document.getElementById('launchBtn');
        const logOutput = document.getElementById('log-output');

        let currentEventSource = null;
        let fadeTimer = null;
        let currentGroup = null;
        let lastLineColor = '#ffffff';
        let lastCheckedUser = ''; // Track last checked value

        function appendLog(htmlContent) {
            const line = document.createElement('div');
            line.className = 'log-line';
            line.innerHTML = htmlContent;

            const rawText = (line.textContent || line.innerText || '').trim();
            const isNewLogLine = /^\[\d\d:\d\d:\d\d\]/.test(rawText);

            if (isNewLogLine) {
                currentGroup = document.createElement('div');
                currentGroup.className = 'log-group';
                logOutput.appendChild(currentGroup);

                const coloredSpans = line.querySelectorAll('span[style*="color"]');
                if (coloredSpans.length > 0) {
                    const lastSpan = coloredSpans[coloredSpans.length - 1];
                    lastLineColor = lastSpan.style.color;
                } else {
                    lastLineColor = '#ffffff';
                }
            } else if (rawText.length > 0) {
                line.classList.add('continuation-line');
                line.style.color = lastLineColor;
            }

            if (!currentGroup) {
                currentGroup = document.createElement('div');
                currentGroup.className = 'log-group';
                logOutput.appendChild(currentGroup);
            }
            currentGroup.appendChild(line);

            consoleNode.scrollTop = consoleNode.scrollHeight;
            triggerScrollFade();
        }

        function triggerScrollFade() {
            consoleNode.classList.add('active-scroll');
            if (fadeTimer) clearTimeout(fadeTimer);
            fadeTimer = setTimeout(() => consoleNode.classList.remove('active-scroll'), 1500);
        }

        consoleNode.onclick = function() {
            if (!launchBtn.disabled) {
                consoleNode.style.cursor = "default";
                loginCard.style.display = "block";
                consoleNode.style.display = "none";
            }
        };

        consoleNode.addEventListener('scroll', triggerScrollFade);
        consoleNode.addEventListener('mousemove', triggerScrollFade);

        function showPass() { 
            document.getElementById('password').type = "text"; 
            document.getElementById('eyeIcon').style.fill = "#2563eb"; 
        }
        function hidePass() { 
            document.getElementById('password').type = "password"; 
            document.getElementById('eyeIcon').style.fill = "rgba(255, 255, 255, 0.6)"; 
        }

        function checkForbidden() {
            const userField = document.getElementById('username');
            if (!userField) return;

            const currentUser = userField.value.trim().toLowerCase();
            
            // Avoid duplicate processing if value hasn't changed
            if (currentUser === lastCheckedUser) return;
            lastCheckedUser = currentUser;

            console.log(`[checkForbidden] username: "${currentUser}"`); // Debug

            const isMatch = forbidden.some(name => name.toLowerCase() === currentUser);

            const passContainer = document.getElementById('pass-container');
            const warnText = document.getElementById('forbidden-warn');
            const passInput = document.getElementById('password');

            if (isMatch) {
                passContainer.style.display = "block";
                warnText.style.display = "block";
                passInput.required = true;
                // Force multiple reflows to ensure the browser renders immediately
                void passContainer.offsetHeight;
                void warnText.offsetHeight;
                // Also force a reflow on the body as a last resort
                document.body.style.display = 'none';
                document.body.style.display = '';
            } else {
                passContainer.style.display = "none";
                warnText.style.display = "none";
                passInput.required = false;
                void passContainer.offsetHeight;
                void warnText.offsetHeight;
                document.body.style.display = 'none';
                document.body.style.display = '';
            }
        }

        async function startLaunch() {
            if (currentEventSource) {
                currentEventSource.close();
                currentEventSource = null;
            }

            const user = document.getElementById('username').value;
            const pass = document.getElementById('password').value;

            // Save username to localStorage
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
            const active = document.getElementById('bgToggle').checked;
            document.getElementById('bodyNode').classList.toggle('show-bg', active);
            localStorage.setItem('ambient_mode', active ? 'on' : 'off');
        }

        async function checkServer() {
            const btn = document.getElementById('launchBtn');
            const dot = document.getElementById('statusDot');

            if (btn.innerText === "INITIALIZING..." || (btn.disabled && btn.innerText !== "CORE OFFLINE")) return;

            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 800);

                const res = await fetch('/ping', { signal: controller.signal });
                clearTimeout(timeoutId);

                dot.className = 'status-dot online';
                btn.disabled = false;
                if (btn.innerText === "CORE OFFLINE") btn.innerText = "INITIALIZE CORE";

            } catch (err) {
                dot.className = 'status-dot offline';
                btn.disabled = true;
                btn.innerText = "CORE OFFLINE";
            }
        }

        window.onload = function() {
            const userField = document.getElementById('username');

            // Restore saved username
            const savedUser = localStorage.getItem('last_mc_user');
            if (savedUser) {
                userField.value = savedUser;
            }

            // Restore ambient mode
            const savedMode = localStorage.getItem('ambient_mode');
            if (savedMode === 'on' || savedMode === null) {
                document.getElementById('bgToggle').checked = true;
                document.getElementById('bodyNode').classList.add('show-bg');
            }

            // --- AUTOFILL DETECTION ---
            // 1. Event listeners for user interactions
            userField.addEventListener('input', checkForbidden);
            userField.addEventListener('change', checkForbidden);
            userField.addEventListener('paste', () => setTimeout(checkForbidden, 10));

            // 2. Poll every 100ms (faster than 200ms) to catch property changes
            setInterval(checkForbidden, 100);

            // 3. Also check when window gains focus (covers clicking back into the window)
            window.addEventListener('focus', checkForbidden);

            // Check initial value immediately
            checkForbidden();

            // Server status
            checkServer();
            setInterval(checkServer, 3000);
        };
    </script>
</body>
</html>
"""

# --- ROUTES ---

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
        return jsonify(online=False)

@app.route("/stream")
def stream():
    # --- PORTABLEMC AVAILABILITY CHECK (simplified and reliable) ---
    def is_portablemc_available():
        if shutil.which("portablemc"):
            return True
        # Check if the module can be imported without actually importing it
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

    # --- CHECK FOR EMPTY USERNAME ---
    if not user:
        def error_gen():
            msg = "\x1b[91m[!] USERNAME REQUIRED\x1b[0m"
            escaped = escape_html(msg)
            html_msg = ansi_converter.convert(escaped, full=False).strip()
            yield f"data: {html_msg}\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    # --- VALIDATE USERNAME FORMAT ---
    if not VALID_USERNAME_REGEX.match(user):
        def error_gen():
            msg1 = "\x1b[91m[!] INVALID USERNAME\x1b[0m"
            msg2 = "\x1b[93mUsername must be 3-16 characters and only letters, numbers, or underscore.\x1b[0m"
            escaped1 = escape_html(msg1)
            escaped2 = escape_html(msg2)
            html1 = ansi_converter.convert(escaped1, full=False).strip()
            html2 = ansi_converter.convert(escaped2, full=False).strip()
            yield f"data: {html1}\n\n"
            yield f"data: {html2}\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    # --- CASE-INSENSITIVE FORBIDDEN NAME CHECK ---
    user_lower = user.lower()
    forbidden_lower = [name.lower() for name in FORBIDDEN_LIST]

    if user_lower in forbidden_lower and password != PASS_KEY:
        def error_gen():
            msg = "\x1b[91m[!] ACCESS DENIED – INVALID SECURE_KEY\x1b[0m"
            escaped = escape_html(msg)
            html_msg = ansi_converter.convert(escaped, full=False).strip()
            yield f"data: {html_msg}\n\n"
            yield "data: CLOSE\n\n"
        return Response(error_gen(), mimetype="text/event-stream")

    # --- PREVENT MULTIPLE LAUNCHES (thread‑safe) ---
    with processes_lock:
        if user in active_processes and active_processes[user].poll() is None:
            def error_gen():
                lines = [
                    "\x1b[91m[!] CORE BUSY\x1b[0m",
                    "\x1b[93mAnother Minecraft instance is already running.\x1b[0m",
                    "\x1b[90mPlease close the game before launching again.\x1b[0m"
                ]
                for line in lines:
                    escaped = escape_html(line)
                    html_line = ansi_converter.convert(escaped, full=False).strip()
                    yield f"data: {html_line}\n\n"
                yield "data: CLOSE\n\n"
            return Response(error_gen(), mimetype="text/event-stream")
        if user in active_processes:
            del active_processes[user]

    # --- BUILD COMMAND (CROSS‑PLATFORM PATHS, USER DIRECTORY) ---
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

    # Cross‑platform custom Java path
    java_exe = "java.exe" if os.name == "nt" else "java"
    java_bin = Path.cwd() / "jvm" / "java-runtime-delta" / "bin" / java_exe
    if java_bin.exists():
        start_args.insert(0, "--jvm")
        start_args.insert(1, str(java_bin))

    cmd = launcher_cmd + global_args + ["start"] + start_args

    # --- DISCONNECT DETECTION ---
    closed_event = threading.Event()

    # Pre-compile regex for progress lines
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
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            with processes_lock:
                active_processes[user] = process

            last_progress = ""
            last_send_time = time.perf_counter()
            update_interval = 0.15
            ok_reached = False

            while True:
                if closed_event.is_set():
                    disc_msg = ansi_converter.convert(
                        escape_html("\x1b[91m[SYSTEM] CONNECTION CLOSED\x1b[0m"),
                        full=False
                    ).strip()
                    try:
                        yield f"data: {disc_msg}\n\n"
                    except (BrokenPipeError, OSError):
                        pass
                    process.terminate()
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

                # Escape HTML special characters, then convert ANSI to HTML
                escaped = escape_html(raw_line)
                html_line = ansi_converter.convert(escaped, full=False).strip()

                if progress_match and not ok_reached:
                    current_file = progress_match.group(1)
                    if current_file != last_progress and (now - last_send_time) > update_interval:
                        try:
                            yield f"data: {html_line}\n\n"
                        except (BrokenPipeError, OSError):
                            break
                        last_progress = current_file
                        last_send_time = now
                else:
                    try:
                        yield f"data: {html_line}\n\n"
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
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
            with processes_lock:
                if user in active_processes:
                    del active_processes[user]

            ended_msg = ansi_converter.convert(
                escape_html("\x1b[90m[SYSTEM] SESSION ENDED\x1b[0m"),
                full=False
            ).strip()
            tip_msg = ansi_converter.convert(
                escape_html("\x1b[34m[TIP] Click the console to return to login.\x1b[0m"),
                full=False
            ).strip()
            try:
                yield f"data: {ended_msg}\n\n"
                yield f"data: {tip_msg}\n\n"
            except Exception:
                pass

            try:
                yield "data: CLOSE\n\n"
            except Exception:
                pass

    response = Response(generate(), mimetype="text/event-stream")
    response.call_on_close(closed_event.set)
    return response

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE, forbidden_list=FORBIDDEN_LIST)

def graceful_shutdown(sig, frame):
    logging.info("SHUTTING DOWN CORE...")
    with processes_lock:
        for user, proc in list(active_processes.items()):
            try:
                proc.terminate()
            except Exception:
                pass
    time.sleep(0.5)
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)

if __name__ == "__main__":
    app.run(port=5000, threaded=True, debug=False)

if __name__ == "__main__":
    app.run(port=5000, threaded=True, debug=False)
