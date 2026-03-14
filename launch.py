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
        * { box-sizing: border-box; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); }
        
        html, body { 
            height: 100%; margin: 0; padding: 0; width: 100%; 
            font-family: 'Segoe UI', system-ui, sans-serif;
            background-color: #080808;
            overflow: hidden;
        }

        body.show-bg {
            background: radial-gradient(circle, rgba(0,0,0,0.2) 0%, rgba(0,0,0,0.6) 100%), 
                        url('/static/bg.png') no-repeat center center fixed;
            background-size: cover;
        }

        .blur-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
            z-index: 0; pointer-events: none; display: none;
        }
        body.show-bg .blur-overlay { display: block; }

        .main-container {
            position: relative; z-index: 10;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            height: 100vh; width: 100%;
            margin-top: -5vh; 
            animation: appEntry 1s ease-out;
        }

        @keyframes appEntry {
            0% { opacity: 0; transform: scale(1.05); filter: blur(10px); }
            100% { opacity: 1; transform: scale(1); filter: blur(0); }
        }

        .top-logo {
            width: 550px; max-width: 90%;
            margin-bottom: 40px;
            filter: drop-shadow(0 20px 40px rgba(0,0,0,0.9));
        }

        @keyframes float {
            0%, 100% { transform: translateY(0px); }
            50% { transform: translateY(-10px); }
        }

        .glass-card {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 20px;
            background: rgba(10, 10, 10, 0.75);
            backdrop-filter: blur(30px) saturate(180%);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 32px;
            width: 420px; 
            padding: 45px 50px; 
            text-align: center;
            color: white;            
            animation: float 6s ease-in-out infinite;
            box-shadow: 0 40px 100px rgba(0, 0, 0, 0.8), 
                        inset 0 0 20px rgba(255, 255, 255, 0.02);
        }

        form {
            display: flex;
            flex-direction: column;
            width: 100%;
        }

        #forbidden-warn {
            color: #ff5555; 
            font-size: 9px; 
            margin: 10px 0 5px 5px; /* Top, Right, Bottom, Left spacing */
            font-weight: 800; 
            text-align: left;
            text-transform: uppercase;
            animation: pulse-red 1.5s infinite;
        }

        @keyframes pulse-red { 
            0%, 100% { opacity: 1; transform: scale(1); } 
            50% { opacity: 0.7; transform: scale(0.98); } 
        }

        .status-container {
            display: flex;
            align-items: center; /* Vertical center */
            justify-content: center;
            gap: 12px;
            margin-bottom: 30px;
            line-height: 1; /* Ensures the text height matches the dot */
        }

        .status-dot {
            width: 8px; 
            height: 8px; 
            border-radius: 50%;
            flex-shrink: 0; /* Prevents dot from squishing */
        }
        .status-dot.online { background: #00ff88; box-shadow: 0 0 12px #00ff88; }
        .status-dot.offline { background: #ff4444; box-shadow: 0 0 12px #ff4444; }

        .subtitle { font-size: 10px; letter-spacing: 4px; text-transform: uppercase; color: rgba(255,255,255,0.4); font-weight: 900; }

        .input-wrapper {
            border: none !important;
            box-shadow: none !important;
        }

        input {
            width: 100%; 
            padding: 18px 22px;
            /* Low alpha (0.05) ensures the blur is visible and not solid */
            background: rgba(255, 255, 255, 0.05) !important; 
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            border-radius: 14px;
            color: white !important;
            
            /* High-visibility typing cursor */
            caret-color: white !important; 
            
            /* Kills the 2-3px offset and browser-specific 'shimmer' */
            outline: none !important;
            box-shadow: none !important; 
            background-clip: padding-box;
            
            /* Pro-level glass effect */
            backdrop-filter: blur(12px) saturate(150%);
            -webkit-backdrop-filter: blur(12px) saturate(150%);
            
            /* Force the OS to stop styling the bar */
            appearance: none;
            -webkit-appearance: none;
            
            transition: all 0.3s ease;
        }

        input:focus {
            /* Keep it sharp on focus - only change the border color, no shadow */
            border-color: rgba(37, 99, 235, 0.6) !important;
            background: rgba(255, 255, 255, 0.06) !important;
        }

        /* Force Autofill to be Translucent Glass */
        input:-webkit-autofill {
            -webkit-text-fill-color: white !important;
            -webkit-box-shadow: 0 0 0px 1000px rgba(15, 15, 15, 0.85) inset !important;
            transition: background-color 5000s ease-in-out 0s;
        }

        input:focus {
            background: rgba(255, 255, 255, 0.1) !important;
            border-color: #2563eb !important;
            box-shadow: 0 0 20px rgba(37, 99, 235, 0.2);
        }

        .toggle-pass {
            position: absolute; 
            right: 18px; 
            top: 50%;
            transform: translateY(-50%); /* Perfectly centers it vertically */
            cursor: pointer; 
            display: flex; 
            align-items: center;
            z-index: 10;
        }

        .toggle-pass svg { 
            width: 18px; 
            height: 18px; 
            fill: #2563eb !important; /* Force blue by default */
            opacity: 0.6;
            transition: opacity 0.3s ease, fill 0.3s ease;
        }
        
        .toggle-pass:hover svg { 
            opacity: 1; 
            fill: #fff !important; /* Turn white on hover */
            filter: drop-shadow(0 0 8px #2563eb);
        }

        button {
            width: 100%; background: #2563eb; color: white; border: none;
            padding: 20px; border-radius: 16px;
            font-weight: 800; font-size: 14px; cursor: pointer;
            letter-spacing: 1px; margin-top: 10px;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1), background 0.5s ease;
        }

        button:hover:not(:disabled) { background: #1d4ed8; transform: translateY(-3px); box-shadow: 0 15px 35px rgba(37, 99, 235, 0.4); }

        button:disabled {
            /* Muted but visible dark red */
            background: rgba(180, 0, 0, 0.15) !important;
            color: rgba(255, 100, 100, 0.6) !important; /* Lighter red text for visibility */
            
            /* Sharp, thin red border to define the shape */
            border: 1px solid rgba(255, 0, 0, 0.2) !important;
            
            /* Text shadow makes the "Core Offline" readable on black */
            text-shadow: 0 0 10px rgba(255, 0, 0, 0.4);
            
            /* Desaturate the button so it looks "unpowered" */
            filter: saturate(0.5); 
            
            box-shadow: inset 0 0 20px rgba(0, 0, 0, 0.5) !important;
            cursor: not-allowed;
            transform: scale(0.98) !important;
            transition: all 0.4s ease;
        }

        /* --- HIGH-PERFORMANCE LIVE CONSOLE --- */
        #console-container { 
            display: none; 
            width: 85%; 
            height: 65%; 
            background: rgba(0,0,0,0.9); 
            backdrop-filter: blur(25px); 
            border: 1px solid rgba(255,255,255,0.1); 
            border-radius: 32px; 
            padding: 35px; 
            overflow-y: auto; 
            text-align: left;
            font-family: 'Consolas', monospace; 
            color: #ffffff; /* Default white text */
            box-shadow: 0 50px 100px rgba(0,0,0,0.8);
            
            /* Interaction & Scrollbar Base */
            cursor: default;
            scrollbar-width: thin;
            scrollbar-color: rgba(255, 255, 255, 0) transparent;
            
            /* Unified Transition */
            transition: 
                scrollbar-color 0.8s ease-in-out, 
                transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), 
                box-shadow 0.3s ease,
                border-color 0.3s ease;
        }

        #console-container:hover {
            transform: scale(1.01);
            border-color: rgba(255, 255, 255, 0.2);
            box-shadow: 0 70px 120px rgba(0, 0, 0, 1), 0 0 30px rgba(0, 255, 136, 0.05);
        }

        /* --- SCROLLBAR STATES --- */
        #console-container.active-scroll {
            scrollbar-color: rgba(255, 255, 255, 0.25) transparent;
        }

        #console-container::-webkit-scrollbar {
            width: 5px;
        }

        #console-container::-webkit-scrollbar-thumb {
            background-color: rgba(255, 255, 255, 0);
            border-radius: 10px;
            transition: background-color 0.8s ease; 
        }

        #console-container.active-scroll::-webkit-scrollbar-thumb {
            background-color: rgba(255, 255, 255, 0.25);
        }

        .log-group {
            border-left: 2px solid rgba(255, 255, 255, 0.2);
            margin-bottom: 4px;
            background: none; 
        }

        .log-line { 
            font-size: 13px; 
            padding-left: 15px; 
            transition: none;
            line-height: 1.4;
            background: none;
            margin: 0;
        }

        /* Continuation lines: subtle indent */
        .log-line.continuation-line {
            padding-left: 17px;
            opacity: 0.95;
        }

        /* --- AMBIENT MODE WIDGET --- */
        .controls {
            position: fixed; 
            bottom: 40px; 
            left: 40px; /* Pushed away from corners to prevent overlap */
            display: flex; 
            align-items: center; 
            gap: 15px; 
            z-index: 100;
            background: rgba(10, 10, 10, 0.6); 
            backdrop-filter: blur(15px); 
            padding: 12px 20px; 
            border-radius: 24px;
            border: 1px solid rgba(255,255,255,0.08);
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }

        .switch { position: relative; width: 34px; height: 18px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
            position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background-color: rgba(255,255,255,0.1); transition: .4s; border-radius: 34px;
        }
        .slider:before {
            position: absolute; content: ""; height: 12px; width: 12px;
            left: 3px; bottom: 3px; background-color: #fff; transition: .4s; border-radius: 50%;
        }
        input:checked + .slider { background-color: #2563eb; box-shadow: 0 0 10px rgba(37, 99, 235, 0.4); }
        input:checked + .slider:before { transform: translateX(16px); }

        .control-label { 
            font-size: 9px; 
            color: rgba(255, 255, 255, 0.5); 
            text-transform: uppercase; 
            letter-spacing: 2.5px; 
            font-weight: 800; 
        }

        .watermark { 
            position: fixed; 
            bottom: 40px; 
            right: 40px; 
            font-size: 9px; 
            color: rgba(255,255,255,0.2); 
            font-weight: 900; 
            letter-spacing: 4px; 
            pointer-events: none;
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

        function appendLog(htmlContent) {
            const line = document.createElement('div');
            line.className = 'log-line';
            line.innerHTML = htmlContent;

            // Get plain text content for detection
            const rawText = (line.textContent || line.innerText || '').trim();
            const isNewLogLine = /^\[\d\d:\d\d:\d\d\]/.test(rawText);

            if (isNewLogLine) {
                // This is a new log entry – create a new group container
                currentGroup = document.createElement('div');
                currentGroup.className = 'log-group';
                logOutput.appendChild(currentGroup);

                // Update the stored color from this line's content
                const coloredSpans = line.querySelectorAll('span[style*="color"]');
                if (coloredSpans.length > 0) {
                    const lastSpan = coloredSpans[coloredSpans.length - 1];
                    lastLineColor = lastSpan.style.color;
                } else {
                    lastLineColor = '#ffffff';
                }
            } else if (rawText.length > 0) {
                // This is a continuation line – add the class and apply previous line's color
                line.classList.add('continuation-line');
                line.style.color = lastLineColor;
            }

            // Append the line to the current group (or create a fallback group if none exists)
            if (!currentGroup) {
                currentGroup = document.createElement('div');
                currentGroup.className = 'log-group';
                logOutput.appendChild(currentGroup);
            }
            currentGroup.appendChild(line);

            // Scroll to bottom
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

        function showPass() { document.getElementById('password').type = "text"; document.getElementById('eyeIcon').style.fill = "#fff"; }
        function hidePass() { document.getElementById('password').type = "password"; document.getElementById('eyeIcon').style.fill = "#2563eb"; }

        function checkForbidden() {
            const userField = document.getElementById('username');
            if (!userField) return;

            const user = userField.value.trim().toLowerCase();
            const isMatch = forbidden.some(name => name.toLowerCase() === user);
            
            const passContainer = document.getElementById('pass-container');
            const warnText = document.getElementById('forbidden-warn');
            const passInput = document.getElementById('password');

            if (isMatch) {
                passContainer.style.display = "block";
                warnText.style.display = "block";
                passInput.required = true;
            } else {
                passContainer.style.display = "none";
                warnText.style.display = "none";
                passInput.required = false;
            }
        }

        async function startLaunch() {
            // Close any previous EventSource to avoid conflicts
            if (currentEventSource) {
                currentEventSource.close();
                currentEventSource = null;
            }

            const user = document.getElementById('username').value;
            const pass = document.getElementById('password').value;
            
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

            const savedUser = localStorage.getItem('last_mc_user');
            if (savedUser) { 
                userField.value = savedUser; 
            }

            const savedMode = localStorage.getItem('ambient_mode');
            if (savedMode === 'on' || savedMode === null) { 
                document.getElementById('bgToggle').checked = true; 
                document.getElementById('bodyNode').classList.add('show-bg'); 
            }

            userField.addEventListener('input', checkForbidden);
            userField.addEventListener('change', checkForbidden);
            userField.addEventListener('paste', () => setTimeout(checkForbidden, 10));

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
    except:
        return jsonify(online=False)

@app.route("/stream")
def stream():	
    # --- PORTABLEMC AVAILABILITY CHECK (simplified and reliable) ---
    def is_portablemc_available():
        if shutil.which("portablemc"):
            return True
        try:
            import portablemc
            return True
        except ImportError:
            return False

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
                    except:
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
                except:
                    pass
        except Exception as e:
            if not closed_event.is_set():
                try:
                    yield f"data: [SYSTEM ERROR] {str(e)}\n\n"
                except:
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
            except:
                pass

            try:
                yield "data: CLOSE\n\n"
            except:
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
            except:
                pass
    time.sleep(0.5)
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)

if __name__ == "__main__":
    app.run(port=5000, threaded=True, debug=False)
