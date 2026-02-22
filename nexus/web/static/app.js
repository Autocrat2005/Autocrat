/* ═══════════════════════════════════════════════════════════════════
   NEXUS OS — Web Control Panel v2.0
   WebSocket, command execution, live updates, tab switching,
   Quick Actions, autocomplete, keyboard shortcuts, accessibility
   ═══════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    // ─── State ───────────────────────────────────────────────────
    let ws = null;
    let commandHistory = [];
    let historyIndex = -1;
    let authToken = localStorage.getItem("nexus_token") || "";
    let allCommands = [];
    let autocompleteVisible = false;
    let autocompleteIndex = -1;
    let confirmationState = null;
    let mediaRecorder = null;
    let recordingChunks = [];
    const API = window.location.origin;
    const WS_PROTO = window.location.protocol === "https:" ? "wss:" : "ws:";

    // ─── DOM Elements ────────────────────────────────────────────
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    const terminalInput = $("#terminal-input");
    const terminalOutput = $("#terminal-output");
    const logOutput = $("#log-output");
    const wsStatus = $("#ws-status");
    const btnSend = $("#btn-send");
    const autocompleteDropdown = $("#autocomplete-dropdown");

    function authHeaders(extra = {}) {
        const headers = { ...extra };
        if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
        return headers;
    }

    function ensureVoiceOverlay() {
        if (document.getElementById("voice-confirm-overlay")) return;
        const overlay = document.createElement("div");
        overlay.id = "voice-confirm-overlay";
        overlay.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(8,12,20,.88);display:none;align-items:center;justify-content:center;padding:18px;";
        overlay.innerHTML = `
          <div style="max-width:560px;width:100%;background:#111827;border:1px solid #1f2937;border-radius:14px;padding:16px;color:#e5e7eb;box-shadow:0 20px 70px rgba(0,0,0,.45)">
            <h3 style="margin:0 0 10px 0;color:#fca5a5">⚠ Awaiting Authorization</h3>
            <div id="voice-confirm-text" style="font-size:14px;line-height:1.5;white-space:pre-wrap"></div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;">
              <button id="btn-confirm-yes" style="padding:10px 14px;border:none;border-radius:10px;background:#16a34a;color:white;cursor:pointer">Approve</button>
              <button id="btn-confirm-no" style="padding:10px 14px;border:none;border-radius:10px;background:#dc2626;color:white;cursor:pointer">Reject</button>
              <button id="btn-voice-hold" style="padding:10px 14px;border:1px solid #334155;border-radius:10px;background:#0f172a;color:#93c5fd;cursor:pointer">Hold to Talk</button>
              <button id="btn-confirm-close" style="padding:10px 14px;border:1px solid #334155;border-radius:10px;background:#111827;color:#e5e7eb;cursor:pointer">Hide</button>
            </div>
            <div id="voice-confirm-status" style="margin-top:10px;color:#93c5fd;font-size:12px"></div>
          </div>
        `;
        document.body.appendChild(overlay);

        const yes = document.getElementById("btn-confirm-yes");
        const no = document.getElementById("btn-confirm-no");
        const close = document.getElementById("btn-confirm-close");
        const hold = document.getElementById("btn-voice-hold");

        yes.addEventListener("click", () => {
            if (!confirmationState) return;
            sendCommand(confirmationState.approve_command || `approve ${confirmationState.id}`);
        });
        no.addEventListener("click", () => {
            if (!confirmationState) return;
            sendCommand(confirmationState.reject_command || `reject ${confirmationState.id}`);
        });
        close.addEventListener("click", () => {
            overlay.style.display = "none";
        });

        hold.addEventListener("mousedown", startVoiceRecording);
        hold.addEventListener("touchstart", (e) => { e.preventDefault(); startVoiceRecording(); }, { passive: false });
        hold.addEventListener("mouseup", stopVoiceRecording);
        hold.addEventListener("mouseleave", stopVoiceRecording);
        hold.addEventListener("touchend", (e) => { e.preventDefault(); stopVoiceRecording(); }, { passive: false });
    }

    function showConfirmationAlert(data) {
        ensureVoiceOverlay();
        confirmationState = data || null;
        const overlay = document.getElementById("voice-confirm-overlay");
        const txt = document.getElementById("voice-confirm-text");
        const status = document.getElementById("voice-confirm-status");
        if (!overlay || !txt) return;

        const reasons = (data.reasons || []).map(r => `• ${r}`).join("\n");
        txt.textContent = `Command: ${data.command || "(unknown)"}\nID: ${data.id || ""}${reasons ? `\n\nReasons:\n${reasons}` : ""}`;
        if (status) status.textContent = "Say: 'Do it' or 'Cancel that' using Hold to Talk.";
        overlay.style.display = "flex";
    }

    async function startVoiceRecording() {
        const status = document.getElementById("voice-confirm-status");
        try {
            if (mediaRecorder && mediaRecorder.state === "recording") return;
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            recordingChunks = [];
            mediaRecorder = new MediaRecorder(stream);
            mediaRecorder.ondataavailable = (e) => {
                if (e.data && e.data.size > 0) recordingChunks.push(e.data);
            };
            mediaRecorder.onstop = async () => {
                const blob = new Blob(recordingChunks, { type: mediaRecorder.mimeType || "audio/webm" });
                stream.getTracks().forEach(t => t.stop());
                await uploadVoice(blob);
            };
            mediaRecorder.start();
            if (status) status.textContent = "🎙 Recording... release to send";
        } catch (err) {
            if (status) status.textContent = `Mic error: ${err.message}`;
        }
    }

    function stopVoiceRecording() {
        if (mediaRecorder && mediaRecorder.state === "recording") {
            mediaRecorder.stop();
        }
    }

    async function uploadVoice(audioBlob) {
        const status = document.getElementById("voice-confirm-status");
        try {
            const fd = new FormData();
            fd.append("audio", audioBlob, "voice.webm");
            if (confirmationState?.id) fd.append("confirmation_id", confirmationState.id);

            const res = await fetch(`${API}/api/voice`, {
                method: "POST",
                headers: authHeaders(),
                body: fd,
            });
            const data = await res.json();
            if (data.success) {
                if (status) status.textContent = `✅ Heard: "${data.transcription}"`;
                if (data.result) displayResult(data.result);
            } else {
                if (status) status.textContent = `❌ Voice failed: ${data.error || "unknown"}`;
            }
        } catch (err) {
            if (status) status.textContent = `❌ Voice upload failed: ${err.message}`;
        }
    }

    // ─── WebSocket Connection ────────────────────────────────────
    function connectWS() {
        const qp = new URLSearchParams();
        qp.set("source", "web");
        if (authToken) qp.set("token", authToken);
        const wsUrl = `${WS_PROTO}//${window.location.host}/ws?${qp.toString()}`;
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            wsStatus.classList.add("status-connected");
            wsStatus.classList.remove("status-disconnected");
            wsStatus.querySelector("span:last-child").textContent = "LIVE";
            addLog("INFO", "system", "WebSocket connected");
        };

        ws.onclose = () => {
            wsStatus.classList.remove("status-connected");
            wsStatus.classList.add("status-disconnected");
            wsStatus.querySelector("span:last-child").textContent = "OFFLINE";
            addLog("WARNING", "system", "WebSocket disconnected, reconnecting...");
            setTimeout(connectWS, 3000);
        };

        ws.onerror = () => {
            addLog("ERROR", "system", "WebSocket error");
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === "command_result") {
                    // Already handled in sendCommand
                } else if (msg.type === "log") {
                    addLog(
                        msg.data.level,
                        msg.data.module,
                        msg.data.message
                    );
                } else if (msg.type === "alert") {
                    showConfirmationAlert(msg.data || {});
                    appendTerminal("warning", `⚠ Confirmation required: ${msg.data?.command || "unknown"}`);
                } else if (msg.type === "voice_result") {
                    const t = msg.data?.transcription;
                    if (t) appendTerminal("meta", `🎙 Heard: ${t}`);
                }
            } catch (e) {
                console.error("WS message parse error:", e);
            }
        };
    }

    // ─── Command Execution ───────────────────────────────────────
    async function sendCommand(text) {
        if (!text.trim()) return;

        // Add command to terminal
        appendTerminal("command", text);

        // Save to local history
        commandHistory.push(text);
        historyIndex = commandHistory.length;

        // Add executing animation
        const termContainer = $(".terminal-container");
        termContainer.classList.add("executing");

        try {
            const headers = { "Content-Type": "application/json" };
            if (authToken) headers["Authorization"] = `Bearer ${authToken}`;

            const res = await fetch(`${API}/api/command`, {
                method: "POST",
                headers: headers,
                body: JSON.stringify({ command: text }),
            });

            const data = await res.json();
            displayResult(data);
        } catch (err) {
            appendTerminal("error", `Connection error: ${err.message}`);
        }

        termContainer.classList.remove("executing");
    }

    function displayResult(data) {
        if (data.success) {
            // Show AI match badge if brain was used
            if (data.ai_match) {
                const pct = Math.round(data.ai_match.confidence * 100);
                appendTerminal(
                    "result",
                    `🧠 AI matched: ${data.ai_match.intent} (${pct}% confidence)`
                );
            }

            const result = data.result || data.results;

            if (typeof result === "string") {
                appendTerminal("result", result);
            } else if (Array.isArray(result)) {
                if (result.length === 0) {
                    appendTerminal("result", "(empty)");
                } else if (typeof result[0] === "object") {
                    appendTerminal("result", formatTable(result));
                } else {
                    appendTerminal("result", result.join("\n"));
                }
            } else if (typeof result === "object" && result !== null) {
                if (data.preview) {
                    appendTerminalWithImage(data.result || "Screenshot captured", data.preview);
                } else {
                    appendTerminal("result", formatObject(result));
                }
            } else {
                appendTerminal("result", String(result));
            }

            // Show duration
            if (data.duration_ms !== undefined) {
                appendTerminal("meta", `⏱ ${data.duration_ms}ms`);
            }
        } else {
            if (data.requires_confirmation) {
                showConfirmationAlert({
                    id: data.confirmation_id,
                    command: data.command || "",
                    reasons: data.reasons || [],
                    approve_command: data.approve_command,
                    reject_command: data.reject_command,
                });
            }
            appendTerminal("error", `✗ ${data.error || "Unknown error"}`);
            if (data.hint) {
                appendTerminal("hint", `💡 ${data.hint}`);
            }
            // Show suggestions as clickable tags
            if (data.suggestions && data.suggestions.length > 0) {
                const sugDiv = document.createElement("div");
                sugDiv.className = "output-line suggestions";
                sugDiv.innerHTML = `<pre>💡 Try: ${data.suggestions.map(s =>
                    `<span class="suggestion-tag" data-cmd="${escapeHtml(s)}">${escapeHtml(s)}</span>`
                ).join(" ")}</pre>`;
                terminalOutput.appendChild(sugDiv);
                sugDiv.querySelectorAll(".suggestion-tag").forEach(tag => {
                    tag.addEventListener("click", () => {
                        terminalInput.value = tag.dataset.cmd;
                        terminalInput.focus();
                    });
                });
                terminalOutput.scrollTop = terminalOutput.scrollHeight;
            }
        }
    }

    function formatObject(obj, indent = 0) {
        const pad = "  ".repeat(indent);
        let lines = [];

        for (const [key, val] of Object.entries(obj)) {
            if (val && typeof val === "object" && !Array.isArray(val)) {
                lines.push(`${pad}${key}:`);
                lines.push(formatObject(val, indent + 1));
            } else if (Array.isArray(val)) {
                if (val.length > 0 && typeof val[0] === "object") {
                    lines.push(`${pad}${key}:`);
                    lines.push(formatTable(val));
                } else {
                    lines.push(`${pad}${key}: ${val.join(", ")}`);
                }
            } else {
                lines.push(`${pad}${key}: ${val}`);
            }
        }

        return lines.join("\n");
    }

    function formatTable(arr) {
        if (!arr.length) return "(empty)";
        const maxRows = 30;
        const items = arr.slice(0, maxRows);

        // Get all keys
        const keys = Object.keys(items[0]);

        // Calculate column widths
        const widths = {};
        keys.forEach((k) => {
            widths[k] = Math.max(
                k.length,
                ...items.map((i) => String(i[k] || "").length)
            );
            widths[k] = Math.min(widths[k], 40); // Max column width
        });

        // Build table
        const header = keys.map((k) => k.padEnd(widths[k])).join("  ");
        const separator = keys.map((k) => "─".repeat(widths[k])).join("──");
        const rows = items.map((item) =>
            keys
                .map((k) => String(item[k] || "").padEnd(widths[k]).slice(0, widths[k]))
                .join("  ")
        );

        let result = [header, separator, ...rows].join("\n");
        if (arr.length > maxRows) {
            result += `\n... and ${arr.length - maxRows} more`;
        }
        return result;
    }

    // ─── Terminal Output ─────────────────────────────────────────
    function appendTerminal(type, text) {
        const div = document.createElement("div");
        div.className = `output-line ${type}`;

        const pre = document.createElement("pre");
        pre.textContent = text;
        div.appendChild(pre);

        terminalOutput.appendChild(div);
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }

    function appendTerminalWithImage(text, imgSrc) {
        const div = document.createElement("div");
        div.className = "output-line result";

        const pre = document.createElement("pre");
        pre.textContent = text;
        div.appendChild(pre);

        const img = document.createElement("img");
        img.src = imgSrc;
        img.className = "screenshot-preview";
        img.alt = "Screenshot";
        div.appendChild(img);

        terminalOutput.appendChild(div);
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }

    // ─── Live Logs ───────────────────────────────────────────────
    function addLog(level, module, message) {
        const div = document.createElement("div");
        div.className = "log-entry";

        const time = new Date().toLocaleTimeString("en-US", {
            hour12: false,
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });

        div.innerHTML = `
            <span class="log-time">${time}</span>
            <span class="log-level ${level}">${level}</span>
            <span class="log-msg">${escapeHtml(message)}</span>
        `;

        logOutput.appendChild(div);

        // Auto-scroll and cap
        while (logOutput.children.length > 200) {
            logOutput.removeChild(logOutput.firstChild);
        }
        logOutput.scrollTop = logOutput.scrollHeight;
    }

    // ─── Tab Switching ───────────────────────────────────────────
    function initTabs() {
        $$(".tab").forEach((tab) => {
            tab.addEventListener("click", () => {
                switchTab(tab.dataset.tab);
            });
        });
    }

    function switchTab(tabName) {
        $$(".tab").forEach((t) => {
            t.classList.remove("active");
            t.setAttribute("aria-selected", "false");
        });
        $$(".tab-content").forEach((c) => c.classList.remove("active"));

        const tab = $(`.tab[data-tab="${tabName}"]`);
        if (tab) {
            tab.classList.add("active");
            tab.setAttribute("aria-selected", "true");
        }
        const content = $(`#tab-${tabName}`);
        if (content) content.classList.add("active");

        // Load data for tabs
        if (tabName === "dashboard") loadDashboard();
        if (tabName === "plugins") loadPlugins();
        if (tabName === "history") loadHistory();
        if (tabName === "workflows") loadWorkflows();
    }

    // ─── Dashboard ───────────────────────────────────────────────
    async function loadDashboard() {
        try {
            const fetches = [fetch(`${API}/api/system`)];
            // Try brain endpoint too
            fetches.push(fetch(`${API}/api/brain`).catch(() => null));
            const [sysRes, brainRes] = await Promise.all(fetches);
            const data = await sysRes.json();

            if (!data.success || !data.result) return;

            const info = data.result;

            // CPU gauge
            if (info.cpu) {
                const cpuPercent = info.cpu.usage_percent || 0;
                setGauge("gauge-cpu", cpuPercent);
                $("#gauge-cpu-text").textContent = `${Math.round(cpuPercent)}%`;
                $("#cpu-details").innerHTML = buildDetails({
                    "Cores": `${info.cpu.cores_physical}P / ${info.cpu.cores_logical}L`,
                    "Frequency": `${info.cpu.frequency_mhz || "--"} MHz`,
                });
                $("#cpu-val").textContent = `${Math.round(cpuPercent)}%`;
            }

            // RAM gauge
            if (info.memory) {
                const ramPercent = info.memory.usage_percent || 0;
                setGauge("gauge-ram", ramPercent);
                $("#gauge-ram-text").textContent = `${Math.round(ramPercent)}%`;
                $("#ram-details").innerHTML = buildDetails({
                    "Used": `${info.memory.used_gb} GB`,
                    "Total": `${info.memory.total_gb} GB`,
                    "Available": `${info.memory.available_gb} GB`,
                });
                $("#ram-val").textContent = `${Math.round(ramPercent)}%`;
            }

            // Disk bars
            if (info.disks) {
                let diskHtml = "";
                info.disks.forEach((d) => {
                    const color = d.usage_percent > 90 ? 'var(--text-error)' : d.usage_percent > 75 ? 'var(--text-warning)' : '';
                    diskHtml += `
                        <div class="disk-item">
                            <div class="disk-label">
                                <span>${d.drive} ${d.filesystem}</span>
                                <span style="${color ? 'color:' + color : ''}">${d.used_gb}/${d.total_gb} GB (${d.usage_percent}%)</span>
                            </div>
                            <div class="disk-bar">
                                <div class="disk-bar-fill ${d.usage_percent > 90 ? 'critical' : d.usage_percent > 75 ? 'warning' : ''}" style="width: ${d.usage_percent}%"></div>
                            </div>
                        </div>
                    `;
                });
                $("#disk-bars").innerHTML = diskHtml;
                const mainDisk = info.disks[0];
                if (mainDisk) {
                    $("#disk-val").textContent = `${mainDisk.usage_percent}%`;
                }
            }

            // Network
            if (info.network) {
                let netHtml = buildDetails({
                    "Sent": info.network.bytes_sent,
                    "Received": info.network.bytes_recv,
                    "Packets ↑": info.network.packets_sent?.toLocaleString(),
                    "Packets ↓": info.network.packets_recv?.toLocaleString(),
                });
                if (info.network.interfaces) {
                    info.network.interfaces.forEach((iface) => {
                        netHtml += `<div class="detail-row"><span>${iface.name}</span><span class="detail-value">${iface.ip}</span></div>`;
                    });
                }
                $("#net-details").innerHTML = netHtml;
            }

            // Battery
            if (info.battery) {
                if (info.battery.percent !== undefined) {
                    $("#battery-details").innerHTML = buildDetails({
                        "Level": `${info.battery.percent}%`,
                        "Plugged In": info.battery.plugged_in ? "Yes ⚡" : "No",
                        "Remaining": info.battery.time_remaining,
                    });
                    // Show battery in top stats
                    const batPill = $("#stat-battery");
                    if (batPill) {
                        batPill.style.display = "flex";
                        $("#bat-val").textContent = `${info.battery.percent}%`;
                    }
                } else {
                    $("#battery-details").innerHTML = `<div class="detail-row">${info.battery.status || "N/A"}</div>`;
                }
            }

            // System
            if (info.system) {
                $("#system-details").innerHTML = buildDetails({
                    "OS": info.system.os,
                    "Host": info.system.hostname,
                    "Processor": info.system.processor?.substring(0, 40),
                    "Uptime": info.uptime?.uptime || "--",
                });
            }

            // AI Brain stats
            if (brainRes) {
                try {
                    const brainData = await brainRes.json();
                    if (brainData.success && brainData.result) {
                        const brain = brainData.result;
                        const stats = brain.learning_stats || {};
                        let brainHtml = buildDetails({
                            "AI Brain": brain.brain_active ? "✅ Active" : "⏳ Loading...",
                            "Commands Learned": stats.total_commands_learned || 0,
                            "Unique Intents": stats.unique_intents_seen || 0,
                            "Learned Sequences": stats.learned_sequences || 0,
                            "Time Patterns": stats.time_patterns || 0,
                        });
                        if (brain.frequent_commands && brain.frequent_commands.length > 0) {
                            brainHtml += `<div class="detail-row" style="margin-top:8px"><span style="font-weight:600">Top Commands</span></div>`;
                            brain.frequent_commands.slice(0, 5).forEach(c => {
                                brainHtml += `<div class="detail-row"><span class="frequent-cmd" data-cmd="${escapeHtml(c.command)}">${escapeHtml(c.command)}</span><span class="detail-value">${c.count}×</span></div>`;
                            });
                        }
                        const brainDetails = $("#brain-details");
                        if (brainDetails) brainDetails.innerHTML = brainHtml;

                        // Make frequent commands clickable
                        $$("#brain-details .frequent-cmd").forEach(el => {
                            el.style.cursor = "pointer";
                            el.style.color = "var(--accent-cyan)";
                            el.addEventListener("click", () => {
                                terminalInput.value = el.dataset.cmd;
                                switchTab("terminal");
                                terminalInput.focus();
                            });
                        });
                    }
                } catch (_) { /* brain endpoint may not exist */ }

                // ── AI Engine card ──
                try {
                    const statusRes = await fetch(`${API}/api/command`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            ...(authToken ? { "Authorization": `Bearer ${authToken}` } : {}),
                        },
                        body: JSON.stringify({ command: "status" }),
                    });
                    const statusData = await statusRes.json();
                    if (statusData.success && statusData.result) {
                        const s = statusData.result;
                        const aiHtml = buildDetails({
                            "ML Brain": s.ai_brain || "unknown",
                            "Gemini LLM": s.gemini_llm || "disabled",
                            "Learned Commands": s.learned_commands || 0,
                            "Cache Hits": s.total_cache_hits || 0,
                            "LLM Conversations": s.llm_conversations || 0,
                            "Total Plugins": s.plugins_loaded || 0,
                            "Total Commands": s.total_commands || 0,
                        });
                        const aiEl = $("#ai-engine-details");
                        if (aiEl) aiEl.innerHTML = aiHtml;
                    }
                } catch (_) { }
            }
        } catch (err) {
            console.error("Dashboard load error:", err);
        }
    }

    function setGauge(id, percent) {
        const circle = $(`#${id}`);
        if (!circle) return;
        const circumference = 2 * Math.PI * 52; // r=52
        const offset = circumference - (percent / 100) * circumference;
        circle.style.strokeDasharray = circumference;
        circle.style.strokeDashoffset = offset;
        // Color coding
        if (percent > 90) {
            circle.style.stroke = "var(--text-error)";
        } else if (percent > 75) {
            circle.style.stroke = "var(--text-warning)";
        }
    }

    function buildDetails(obj) {
        let html = "";
        for (const [k, v] of Object.entries(obj)) {
            html += `<div class="detail-row"><span>${k}</span><span class="detail-value">${v || "--"}</span></div>`;
        }
        return html;
    }

    // ─── Plugins Tab ─────────────────────────────────────────────
    async function loadPlugins() {
        try {
            const res = await fetch(`${API}/api/plugins`);
            const data = await res.json();

            if (!data.success || !data.result) return;

            const container = $("#plugins-list");
            container.innerHTML = "";

            data.result.forEach((plugin) => {
                const card = document.createElement("div");
                card.className = "plugin-card";

                const cmds = plugin.commands
                    .map(
                        (c) =>
                            `<span class="cmd-tag" data-usage="${escapeHtml(c.usage)}" title="${escapeHtml(c.description)}" role="button" tabindex="0">${escapeHtml(c.name)}</span>`
                    )
                    .join("");

                card.innerHTML = `
                    <div class="plugin-header">
                        <div class="plugin-icon" aria-hidden="true">${plugin.icon}</div>
                        <div>
                            <div class="plugin-name">${escapeHtml(plugin.name)}</div>
                            <div class="plugin-version">v${escapeHtml(plugin.version)}</div>
                        </div>
                        <span class="plugin-cmd-count">${plugin.commands.length} cmds</span>
                    </div>
                    <div class="plugin-desc">${escapeHtml(plugin.description)}</div>
                    <div class="plugin-commands">${cmds}</div>
                `;

                // Click command tags to auto-fill terminal
                card.querySelectorAll(".cmd-tag").forEach((tag) => {
                    const handler = () => {
                        terminalInput.value = tag.dataset.usage;
                        switchTab("terminal");
                        terminalInput.focus();
                    };
                    tag.addEventListener("click", handler);
                    tag.addEventListener("keydown", (e) => {
                        if (e.key === "Enter" || e.key === " ") handler();
                    });
                });

                container.appendChild(card);
            });
        } catch (err) {
            console.error("Plugins load error:", err);
        }
    }

    // ─── History Tab ─────────────────────────────────────────────
    async function loadHistory() {
        try {
            const res = await fetch(`${API}/api/history`);
            const data = await res.json();

            const container = $("#history-list");
            container.innerHTML = "";

            if (!data.success || !data.result || !data.result.length) {
                container.innerHTML = '<p class="loading">No command history yet</p>';
                return;
            }

            data.result.reverse().forEach((item) => {
                const div = document.createElement("div");
                div.className = "history-item";
                div.setAttribute("role", "button");
                div.setAttribute("tabindex", "0");
                div.innerHTML = `
                    <div class="history-status ${item.success ? "success" : "fail"}"></div>
                    <div class="history-cmd">${escapeHtml(item.command)}</div>
                    <div class="history-duration">${item.duration_ms ? item.duration_ms + "ms" : ""}</div>
                `;

                const handler = () => {
                    terminalInput.value = item.command;
                    switchTab("terminal");
                    terminalInput.focus();
                };
                div.addEventListener("click", handler);
                div.addEventListener("keydown", (e) => {
                    if (e.key === "Enter" || e.key === " ") handler();
                });

                container.appendChild(div);
            });
        } catch (err) {
            console.error("History load error:", err);
        }
    }

    // ─── Workflows Tab ───────────────────────────────────────────
    async function loadWorkflows() {
        try {
            const res = await fetch(`${API}/api/command`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    ...(authToken ? { "Authorization": `Bearer ${authToken}` } : {}),
                },
                body: JSON.stringify({ command: "workflow list" }),
            });

            const data = await res.json();
            const container = $("#workflows-list");
            container.innerHTML = "";

            if (!data.success || !data.result || typeof data.result === "string") {
                container.innerHTML = `<p class="loading">${data.result || "No workflows yet"}</p>`;
                return;
            }

            data.result.forEach((wf) => {
                const div = document.createElement("div");
                div.className = "workflow-item";
                div.innerHTML = `
                    <div>
                        <div class="workflow-name">🔗 ${escapeHtml(wf.name)}</div>
                        <div class="workflow-meta">${wf.steps} steps • Created: ${escapeHtml(wf.created || "")}</div>
                    </div>
                    <div class="workflow-actions">
                        <button class="btn-run" data-name="${escapeHtml(wf.name)}" aria-label="Run workflow ${escapeHtml(wf.name)}">▶ Run</button>
                    </div>
                `;

                div.querySelector(".btn-run").addEventListener("click", () => {
                    sendCommand(`workflow run ${wf.name}`);
                    switchTab("terminal");
                });

                container.appendChild(div);
            });
        } catch (err) {
            console.error("Workflows load error:", err);
        }
    }

    // ─── Utilities ───────────────────────────────────────────────
    function escapeHtml(str) {
        if (typeof str !== "string") return str;
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // ─── Auto-refresh Stats ──────────────────────────────────────
    async function refreshTopStats() {
        try {
            const res = await fetch(`${API}/api/system`);
            const data = await res.json();

            if (!data.success || !data.result) return;

            const info = data.result;
            if (info.cpu) $("#cpu-val").textContent = `${Math.round(info.cpu.usage_percent)}%`;
            if (info.memory) $("#ram-val").textContent = `${Math.round(info.memory.usage_percent)}%`;
            if (info.disks && info.disks[0]) $("#disk-val").textContent = `${info.disks[0].usage_percent}%`;
            if (info.battery && info.battery.percent !== undefined) {
                const batPill = $("#stat-battery");
                if (batPill) {
                    batPill.style.display = "flex";
                    $("#bat-val").textContent = `${info.battery.percent}%`;
                }
            }
        } catch (err) {
            // silent
        }
    }

    // ─── Autocomplete ────────────────────────────────────────────
    async function loadAllCommands() {
        try {
            const res = await fetch(`${API}/api/commands`);
            const data = await res.json();
            if (data.success && data.result) {
                allCommands = data.result.map(c => ({
                    usage: c.usage,
                    description: c.description,
                    plugin: c.plugin,
                    icon: c.icon || "⚡",
                }));
            }
        } catch (err) {
            console.error("Failed to load commands:", err);
        }
    }

    function showAutocomplete(query) {
        if (!query || query.length < 1 || !autocompleteDropdown) {
            hideAutocomplete();
            return;
        }

        const q = query.toLowerCase();
        const matches = allCommands
            .filter(c => c.usage.toLowerCase().includes(q) || c.description.toLowerCase().includes(q))
            .slice(0, 8);

        if (matches.length === 0) {
            hideAutocomplete();
            return;
        }

        autocompleteDropdown.innerHTML = matches.map((c, i) =>
            `<div class="ac-item${i === autocompleteIndex ? ' selected' : ''}" data-usage="${escapeHtml(c.usage)}" data-index="${i}">
                <span class="ac-icon">${c.icon}</span>
                <span class="ac-usage">${escapeHtml(c.usage)}</span>
                <span class="ac-desc">${escapeHtml(c.description)}</span>
            </div>`
        ).join("");

        autocompleteDropdown.hidden = false;
        autocompleteVisible = true;
        terminalInput.setAttribute("aria-expanded", "true");

        autocompleteDropdown.querySelectorAll(".ac-item").forEach(item => {
            item.addEventListener("click", () => {
                terminalInput.value = item.dataset.usage;
                hideAutocomplete();
                terminalInput.focus();
            });
        });
    }

    function hideAutocomplete() {
        if (autocompleteDropdown) {
            autocompleteDropdown.hidden = true;
        }
        autocompleteVisible = false;
        autocompleteIndex = -1;
        terminalInput.setAttribute("aria-expanded", "false");
    }

    // ─── Quick Actions ───────────────────────────────────────────
    function initQuickActions() {
        $$(".quick-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const cmd = btn.dataset.cmd;
                if (cmd) {
                    sendCommand(cmd);
                    // Visual feedback
                    btn.classList.add("quick-btn-active");
                    setTimeout(() => btn.classList.remove("quick-btn-active"), 300);
                }
            });
        });
    }

    // ─── Keyboard Shortcuts ──────────────────────────────────────
    function initKeyboardShortcuts() {
        document.addEventListener("keydown", (e) => {
            const inInput = document.activeElement.tagName === "INPUT" ||
                           document.activeElement.tagName === "TEXTAREA";

            // "/" to focus terminal input
            if (e.key === "/" && !inInput) {
                e.preventDefault();
                switchTab("terminal");
                terminalInput.focus();
                return;
            }

            // Escape to close autocomplete or blur input
            if (e.key === "Escape") {
                if (autocompleteVisible) {
                    hideAutocomplete();
                } else if (inInput) {
                    document.activeElement.blur();
                }
                return;
            }

            // Ctrl+number for tab switching
            if (e.ctrlKey && e.key >= "1" && e.key <= "6" && !e.shiftKey && !e.altKey) {
                e.preventDefault();
                const tabs = ["terminal", "quick", "dashboard", "plugins", "history", "workflows"];
                const idx = parseInt(e.key) - 1;
                if (idx < tabs.length) switchTab(tabs[idx]);
                return;
            }

            // Ctrl+L to clear terminal
            if (e.ctrlKey && e.key === "l" && !e.shiftKey) {
                e.preventDefault();
                terminalOutput.innerHTML = "";
                return;
            }
        });
    }

    // ─── Cmd hint click handlers ─────────────────────────────────
    function initCmdHints() {
        $$(".cmd-hint").forEach(hint => {
            hint.addEventListener("click", () => {
                terminalInput.value = hint.textContent;
                terminalInput.focus();
            });
            hint.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") {
                    terminalInput.value = hint.textContent;
                    terminalInput.focus();
                }
            });
        });
    }

    // ─── Event Listeners ─────────────────────────────────────────
    function init() {
        initTabs();
        initQuickActions();
        initKeyboardShortcuts();
        connectWS();
        loadAllCommands();
        initCmdHints();

        // Terminal input
        terminalInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                if (autocompleteVisible && autocompleteIndex >= 0) {
                    const selected = autocompleteDropdown.querySelector(".ac-item.selected");
                    if (selected) {
                        terminalInput.value = selected.dataset.usage;
                        hideAutocomplete();
                        return;
                    }
                }
                hideAutocomplete();
                sendCommand(terminalInput.value);
                terminalInput.value = "";
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                if (autocompleteVisible) {
                    const items = autocompleteDropdown.querySelectorAll(".ac-item");
                    if (items.length > 0) {
                        autocompleteIndex = Math.max(0, autocompleteIndex - 1);
                        items.forEach((it, i) => it.classList.toggle("selected", i === autocompleteIndex));
                    }
                } else {
                    if (historyIndex > 0) {
                        historyIndex--;
                        terminalInput.value = commandHistory[historyIndex] || "";
                    }
                }
            } else if (e.key === "ArrowDown") {
                e.preventDefault();
                if (autocompleteVisible) {
                    const items = autocompleteDropdown.querySelectorAll(".ac-item");
                    if (items.length > 0) {
                        autocompleteIndex = Math.min(items.length - 1, autocompleteIndex + 1);
                        items.forEach((it, i) => it.classList.toggle("selected", i === autocompleteIndex));
                    }
                } else {
                    if (historyIndex < commandHistory.length - 1) {
                        historyIndex++;
                        terminalInput.value = commandHistory[historyIndex] || "";
                    } else {
                        historyIndex = commandHistory.length;
                        terminalInput.value = "";
                    }
                }
            } else if (e.key === "Tab" && autocompleteVisible) {
                e.preventDefault();
                const selected = autocompleteDropdown.querySelector(".ac-item.selected") ||
                                autocompleteDropdown.querySelector(".ac-item");
                if (selected) {
                    terminalInput.value = selected.dataset.usage;
                    hideAutocomplete();
                }
            } else if (e.key === "Escape") {
                hideAutocomplete();
            }
        });

        // Autocomplete on input
        terminalInput.addEventListener("input", () => {
            const q = terminalInput.value.trim();
            if (q.length >= 1) {
                autocompleteIndex = -1;
                showAutocomplete(q);
            } else {
                hideAutocomplete();
            }
        });

        btnSend.addEventListener("click", () => {
            hideAutocomplete();
            sendCommand(terminalInput.value);
            terminalInput.value = "";
        });

        // Clear buttons
        $("#clear-terminal").addEventListener("click", () => {
            terminalOutput.innerHTML = "";
        });

        $("#clear-logs").addEventListener("click", () => {
            logOutput.innerHTML = "";
        });

        // Refresh buttons
        $("#refresh-history").addEventListener("click", loadHistory);
        $("#refresh-workflows").addEventListener("click", loadWorkflows);

        // Auto-refresh stats every 10s
        setInterval(refreshTopStats, 10000);
        refreshTopStats();

        // ─── Auth Modal ──────────────────────────────────────────
        const authOverlay = $("#auth-overlay");
        const authInput = $("#auth-token");
        const authBtn = $("#btn-auth");
        const authError = $("#auth-error");

        async function verifyToken(token) {
            try {
                const res = await fetch(`${API}/api/auth/verify`, {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${token}` },
                });
                const data = await res.json();
                return data.success;
            } catch {
                return false;
            }
        }

        async function handleLogin() {
            const token = authInput.value.trim();
            if (!token) {
                authError.textContent = "Please enter a token";
                return;
            }
            authError.textContent = "Verifying...";
            authBtn.disabled = true;

            const valid = await verifyToken(token);
            if (valid) {
                authToken = token;
                localStorage.setItem("nexus_token", token);
                authOverlay.classList.add("hidden");
                appendTerminal("result", "🔓 Authenticated — commands are now active");
                // Reconnect WS with token
                if (ws) ws.close();
                connectWS();
            } else {
                authError.textContent = "Invalid token — try again";
                authInput.value = "";
                authInput.focus();
            }
            authBtn.disabled = false;
        }

        authBtn.addEventListener("click", handleLogin);
        authInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleLogin();
        });

        // Check saved token on load
        if (authToken) {
            verifyToken(authToken).then((valid) => {
                if (valid) {
                    authOverlay.classList.add("hidden");
                    appendTerminal("result", "🔓 Auto-authenticated from saved token");
                } else {
                    // Token expired or invalid
                    localStorage.removeItem("nexus_token");
                    authToken = "";
                    authOverlay.classList.remove("hidden");
                }
            });
        }

        // Show shortcut hint briefly on first visit
        if (!localStorage.getItem("nexus_shortcuts_shown")) {
            const hint = $("#shortcut-hint");
            if (hint) {
                hint.hidden = false;
                setTimeout(() => { hint.hidden = true; }, 8000);
                localStorage.setItem("nexus_shortcuts_shown", "1");
            }
        }
    }

    // Go!
    document.addEventListener("DOMContentLoaded", init);
})();
