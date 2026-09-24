// Power button for a SwitchBot Bot, injected into the GL.iNet Comet web UI.
// /switchbot/api/ is forwarded by the Comet's nginx, behind the KVM login,
// to the Bluetooth node that drives the Bot.
(() => {
    if (window.__switchbot) return;
    window.__switchbot = true;

    const API = "/switchbot/api/";
    const CORNERS = ["bottom-left", "bottom-right", "top-right", "top-left"];

    const host = document.createElement("div");
    host.id = "switchbot";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
<style>
:host { all: initial; }
.wrap { position: fixed; z-index: 2147483000; color: #e4e4e7;
        font: 13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap.bottom-left { left: 14px; bottom: 14px; }
.wrap.bottom-right { right: 14px; bottom: 14px; }
.wrap.top-right { right: 14px; top: 64px; }
.wrap.top-left { left: 14px; top: 64px; }
.fab { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center;
       border: 1px solid rgba(255,255,255,.2); background: rgba(24,24,27,.85); color: #e4e4e7;
       cursor: pointer; box-shadow: 0 2px 10px rgba(0,0,0,.4); }
.fab:hover { background: rgba(39,39,42,.95); }
.fab.busy svg { animation: pulse 1s ease-in-out infinite; }
.fab.err { color: #f87171; }
@keyframes pulse { 50% { opacity: .3; } }
.panel { position: absolute; width: 280px; padding: 12px; border-radius: 10px;
         background: rgba(24,24,27,.97); border: 1px solid rgba(255,255,255,.14);
         box-shadow: 0 8px 28px rgba(0,0,0,.5); }
.bottom-left .panel { left: 0; bottom: 50px; }
.bottom-right .panel { right: 0; bottom: 50px; }
.top-right .panel { right: 0; top: 50px; }
.top-left .panel { left: 0; top: 50px; }
.hdr { display: flex; align-items: center; gap: 4px; font-weight: 600; margin-bottom: 6px; }
.hdr .sp { flex: 1; }
.icon { background: none; border: 0; color: #a1a1aa; cursor: pointer; font-size: 14px; padding: 2px 5px; }
.icon:hover { color: #fff; }
.bot { color: #a1a1aa; font-size: 12px; margin-bottom: 8px; }
.row { display: flex; gap: 8px; margin: 6px 0; align-items: center; }
button.act { flex: 1; padding: 9px 6px; border-radius: 6px; border: 1px solid rgba(255,255,255,.2);
             background: #2563eb; color: #fff; cursor: pointer; font: inherit; font-weight: 600; }
button.act.hold { background: #b91c1c; }
button.act.armed { outline: 2px solid #fbbf24; outline-offset: 1px; }
button:disabled { opacity: .5; cursor: default; }
.msg { min-height: 18px; font-size: 12px; margin-top: 4px; word-break: break-word; }
.msg.ok { color: #4ade80; }
.msg.err { color: #f87171; }
details { margin-top: 10px; border-top: 1px solid rgba(255,255,255,.1); padding-top: 8px; }
summary { cursor: pointer; color: #a1a1aa; }
button.sec { flex: 1; padding: 6px; border-radius: 6px; border: 1px solid rgba(255,255,255,.2);
             background: #3f3f46; color: #e4e4e7; cursor: pointer; font: inherit; }
ul { list-style: none; padding: 0; margin: 4px 0; }
li { display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: 12px; }
li span { flex: 1; font-family: ui-monospace, monospace; }
li button { padding: 3px 8px; }
select { background: #3f3f46; color: #e4e4e7; border: 1px solid rgba(255,255,255,.2); border-radius: 4px; }
label { flex: 1; color: #a1a1aa; }
</style>
<div class="wrap">
  <div class="panel" hidden>
    <div class="hdr"><span>Power button</span><span class="sp"></span>
      <button class="icon move" title="Move to another corner">&#x2922;</button>
      <button class="icon close" title="Close">&#x2715;</button></div>
    <div class="bot"></div>
    <div class="row"><button class="act press" title="Short press">Press</button>
      <button class="act hold" title="Press and hold: forces most PCs off"></button></div>
    <div class="msg"></div>
    <details><summary>Setup</summary>
      <div class="row"><button class="sec scan">Scan for Bots</button><button class="sec info">Bot info</button></div>
      <ul class="found"></ul>
      <div class="row"><label>Hold for <select class="holdsel"></select> s</label>
        <button class="sec pw">Password&hellip;</button></div>
    </details>
  </div>
  <button class="fab" title="Power button (SwitchBot)" hidden>
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2.2" stroke-linecap="round"><path d="M12 3v9"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>
  </button>
</div>`;

    const $ = (sel) => root.querySelector(sel);
    const wrap = $(".wrap"), fab = $(".fab"), panel = $(".panel"), msg = $(".msg");
    const pressBtn = $(".press"), holdBtn = $(".hold"), holdSel = $(".holdsel");
    let status = null;
    let busy = false;

    // Keep clicks and keys inside the widget away from GL's handlers, which
    // forward input to the machine behind the KVM.
    for (const type of ["keydown", "keyup", "keypress", "mousedown", "mouseup", "click",
        "dblclick", "wheel", "pointerdown", "pointerup", "contextmenu"]) {
        host.addEventListener(type, (e) => e.stopPropagation());
    }

    let corner = localStorage.getItem("switchbot.corner");
    if (!CORNERS.includes(corner)) corner = CORNERS[0];
    wrap.classList.add(corner);

    for (let s = 1; s <= 15; s++) holdSel.add(new Option(String(s), String(s)));

    class ApiError extends Error {}

    async function api(path, body) {
        const opts = { credentials: "same-origin", headers: {} };
        if (body !== undefined) {
            opts.method = "POST";
            opts.headers["Content-Type"] = "application/json";
            opts.headers["X-Requested-With"] = "switchbot";
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(API + path, opts);
        const text = await r.text();
        if (r.status === 401 && !text.includes("token")) {
            hide();
            throw new ApiError("not logged in");
        }
        if (r.status === 401) throw new ApiError("the Bluetooth node rejected the Comet's token; reinstall");
        if (r.status === 502 || r.status === 504) throw new ApiError("the Bluetooth node is not answering");
        let data;
        try {
            data = JSON.parse(text);
        } catch {
            throw new ApiError(text.trim() || `HTTP ${r.status}`);
        }
        if (!r.ok) throw new ApiError(text.trim() || `HTTP ${r.status}`);
        return data;
    }

    function show(text, kind) {
        msg.textContent = text;
        msg.className = "msg" + (kind ? " " + kind : "");
    }

    function setBusy(on) {
        busy = on;
        fab.classList.toggle("busy", on);
        for (const b of root.querySelectorAll("button.act, button.sec")) b.disabled = on;
    }

    function hide() {
        fab.hidden = true;
        panel.hidden = true;
    }

    function holdSeconds() {
        return status ? status.hold_seconds : Number(holdSel.value) || 5;
    }

    const holdLabel = () => `Hold ${holdSeconds()} s`;

    function render() {
        if (!holdBtn.classList.contains("armed")) holdBtn.textContent = holdLabel();
        if (!status) return;
        holdSel.value = String(status.hold_seconds);
        const last = status.last;
        let line = status.configured ? `Bot ${status.mac}` : "No Bot set up yet; open Setup and scan.";
        if (last && last.battery !== undefined) line += ` \u00b7 battery ${last.battery}%`;
        $(".bot").textContent = line;
        if (last && !msg.textContent) {
            const when = new Date(last.at * 1000).toLocaleTimeString();
            show(`Last ${last.action} at ${when}: ${last.message}`, last.ok ? "" : "err");
        }
        fab.classList.toggle("err", !!(last && !last.ok));
    }

    async function refresh() {
        status = await api("status");
        render();
    }

    // Hidden until the Comet session is logged in; a 401 from the Comet's
    // login check keeps it hidden and it tries again later.
    async function probe() {
        try {
            const r = await fetch(API + "status", { credentials: "same-origin" });
            if (r.ok) {
                status = await r.json();
                fab.hidden = false;
                render();
            } else if (r.status === 502 || r.status === 504) {
                fab.hidden = false;
                fab.classList.add("err");
                show("the Bluetooth node is not answering", "err");
            }
        } catch {
            // Network hiccup; the next probe retries.
        }
    }

    async function act(path, body, okText) {
        setBusy(true);
        show("Working\u2026", "");
        try {
            const res = await api(path, body);
            if (res.ok) show(okText(res), "ok");
            else show(res.message || "failed", "err");
            await refresh();
            return res;
        } catch (e) {
            show(e.message, "err");
        } finally {
            setBusy(false);
        }
    }

    // First click arms the button for 4 s; a second click runs it.
    function armable(btn, label, run) {
        let timer = null;
        const disarm = () => {
            clearTimeout(timer);
            btn.classList.remove("armed");
            btn.textContent = label();
        };
        btn.addEventListener("click", async () => {
            if (busy) return;
            if (!btn.classList.contains("armed")) {
                btn.classList.add("armed");
                btn.textContent = "Confirm";
                timer = setTimeout(disarm, 4000);
                return;
            }
            disarm();
            await run();
        });
    }

    armable(pressBtn, () => "Press", () => act("press", {}, (r) => `Pressed (${r.elapsed} s)`));
    armable(holdBtn, holdLabel, () => {
        const seconds = holdSeconds();
        show(`Holding for ${seconds} s\u2026`, "");
        return act("hold", { seconds }, (r) => `Held for ${r.seconds} s` + (r.warning ? ` (${r.warning})` : ""));
    });

    $(".info").addEventListener("click", () =>
        act("info", {}, (r) => `Battery ${r.battery}%, firmware ${r.firmware}` +
            (r.switch_mode ? "; switch mode is on, use press mode" : "")));

    $(".scan").addEventListener("click", async () => {
        const list = $(".found");
        list.textContent = "";
        const res = await act("scan", { seconds: 8 }, (r) => r.message);
        if (!res || !res.ok) return;
        for (const bot of res.bots) {
            const li = document.createElement("li");
            const label = document.createElement("span");
            label.textContent = `${bot.mac}  ${bot.rssi} dBm  ${bot.battery}%`;
            const use = document.createElement("button");
            use.className = "sec";
            use.textContent = bot.configured ? "In use" : "Use";
            use.disabled = bot.configured;
            use.addEventListener("click", async () => {
                try {
                    await api("config", { mac: bot.mac });
                    await refresh();
                    list.textContent = "";
                    show(`Using ${bot.mac}`, "ok");
                } catch (e) {
                    show(e.message, "err");
                }
            });
            li.append(label, use);
            list.append(li);
        }
    });

    holdSel.addEventListener("change", async () => {
        try {
            await api("config", { hold_seconds: Number(holdSel.value) });
            await refresh();
        } catch (e) {
            show(e.message, "err");
        }
    });

    $(".pw").addEventListener("click", async () => {
        const pw = window.prompt("Password set on the Bot in the SwitchBot app (leave empty to clear):", "");
        if (pw === null) return;
        try {
            await api("config", { password: pw });
            show(pw ? "Password saved" : "Password cleared", "ok");
        } catch (e) {
            show(e.message, "err");
        }
    });

    fab.addEventListener("click", async () => {
        panel.hidden = !panel.hidden;
        if (panel.hidden) return;
        try {
            await refresh();
        } catch (e) {
            show(e.message, "err");
        }
    });
    $(".close").addEventListener("click", () => { panel.hidden = true; });
    $(".move").addEventListener("click", () => {
        wrap.classList.remove(corner);
        corner = CORNERS[(CORNERS.indexOf(corner) + 1) % CORNERS.length];
        wrap.classList.add(corner);
        localStorage.setItem("switchbot.corner", corner);
    });

    // Elements outside the fullscreen element aren't drawn, so follow it.
    document.addEventListener("fullscreenchange", () => {
        const fs = document.fullscreenElement;
        (fs && !["VIDEO", "CANVAS", "IMG"].includes(fs.tagName) ? fs : document.body).appendChild(host);
    });

    document.body.appendChild(host);
    render();
    probe();
    setInterval(() => { if (fab.hidden) probe(); }, 15000);
})();
