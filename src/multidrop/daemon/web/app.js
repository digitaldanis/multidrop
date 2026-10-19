// multidrop web UI. Talks only to the local daemon; peer-supplied strings
// (device and file names, texts) are always inserted as text, never as HTML.
"use strict";

const $ = (sel) => document.querySelector(sel);
const PROTO = { quickshare: "Quick Share", localsend: "LocalSend", airdrop: "AirDrop" };
let token = "";
let groups = [];
let lastSeq = 0;
const jobs = new Map();          // job id -> {info, files: Map(name -> {done,size}), state}
const resend = new Map();        // job id -> {target, files|paths|text, label}, for a retry with PIN
const manual = [];               // targets added by address
const unchecked = new Map();     // offer id -> Set of file ids the user unticked
let selection = [];              // local paths handed over (Explorer "Send to")

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c instanceof Node ? c : String(c ?? ""));
  return e;
}

function human(n) {
  if (n === null || n === undefined) return "size unknown";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return i ? `${n.toFixed(1)} ${units[i]}` : `${n} B`;
}

async function api(path, opts = {}) {
  opts.headers = { ...(opts.headers || {}), "X-Multidrop-Token": token };
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// -- status, offers -------------------------------------------------------

async function loadStatus() {
  const s = await api("/api/status");
  const ports = Object.entries(s.protocols)
    .map(([p, v]) => `${PROTO[p] || p} ${v.port}`).join(" · ");
  // Plain status; the technical details stay in the tooltip.
  $("#status").textContent = `Ready – visible as “${s.alias}”` +
    (s.accept === "all" ? " · accepts everything" : "");
  $("#status").title = `${ports} · saves to ${s.download_dir}`;
  document.querySelectorAll(".alias").forEach((e) => { e.textContent = `“${s.alias}”`; });
}

const PARTIAL_NOTE = {
  discard: "Quick Share can only accept everything: unselected files are transferred but not saved.",
  none: "All or nothing (the protocol does not allow choosing).",
};

async function loadOffers() {
  const offers = await api("/api/offers");
  const box = $("#offers");
  box.replaceChildren();
  $("#offers-section").hidden = offers.length === 0;
  for (const id of unchecked.keys()) if (!offers.some((o) => o.id === id)) unchecked.delete(id);
  for (const o of offers) {
    const selectable = o.partial !== "none" && o.files.length > 1;
    const off = unchecked.get(o.id) || new Set();
    unchecked.set(o.id, off);
    const items = el("ul", { class: selectable ? "files selectable" : "files" });
    for (const f of o.files) {
      const label = f.text !== null ? `Text: ${f.text}` : `${f.name} (${human(f.size)})`;
      if (!selectable) { items.append(el("li", {}, label)); continue; }
      const cb = el("input", { type: "checkbox", "data-file": f.id, onchange: (ev) => {
        if (ev.target.checked) off.delete(f.id); else off.add(f.id);
      } });
      cb.checked = !off.has(f.id);
      items.append(el("li", {}, el("label", {}, cb, " ", label)));
    }
    const answer = (accept, files) => api(`/api/offers/${o.id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(files ? { accept, files } : { accept }),
    }).catch((e) => addLog(`Error: ${e.message}`)).finally(loadOffers);
    const chosen = () => o.files.map((f) => f.id).filter((id) => !off.has(id));
    const note = o.files.length > 1 ? PARTIAL_NOTE[o.partial] : null;
    box.append(el("div", { class: "card offer" },
      el("div", { class: "name" }, `${o.from} wants to send`),
      el("div", { class: "meta" }, `${PROTO[o.protocol] || o.protocol} · ${o.address} · ` +
        (o.size_unknown ? "size unknown" : human(o.total_size))),
      items,
      note ? el("p", { class: "hint" }, note) : "",
      o.pin ? el("div", {}, "Code: ", el("span", { class: "pin" }, o.pin),
                 el("span", { class: "meta" }, " – must match the sender")) : "",
      el("div", { class: "row" },
        el("button", { class: "primary", type: "button", onclick: () => answer(true) },
           selectable ? "Accept all" : "Accept"),
        selectable ? el("button", { type: "button", "data-action": "accept-selected", onclick: () => {
          const ids = chosen();
          if (ids.length) answer(true, ids); else answer(false);
        } }, "Accept selected") : "",
        el("button", { class: "danger", type: "button", onclick: () => answer(false) }, "Decline"))));
  }
}

// -- devices and sending --------------------------------------------------

async function loadDevices(refresh) {
  const btn = $("#refresh");
  btn.disabled = true;
  btn.textContent = refresh ? "Searching …" : "Search";
  try {
    groups = await api("/api/devices" + (refresh ? "?refresh=1&timeout=12" : ""));
  } catch (e) {
    addLog(`Device search failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Search";
  }
  renderDevices();
}

function allGroups() {
  return groups.concat(manual);
}

function renderDevices() {
  const box = $("#devices");
  box.replaceChildren();
  const select = $("#text-target");
  select.replaceChildren();
  for (const g of allGroups()) {
    let chosen = g.targets[0];
    const protoSel = el("select", { onclick: (ev) => ev.stopPropagation(),
                                    onchange: (ev) => { chosen = g.targets[ev.target.value]; } });
    g.targets.forEach((t, i) => protoSel.append(el("option", { value: i }, PROTO[t.protocol] || t.protocol)));
    const card = el("div", { class: "card drop" },
      el("div", { class: "name" }, g.name),
      el("div", { class: "meta" }, g.type ? `${g.type} · ` : "", `${chosen.address}`),
      g.targets.length > 1 ? protoSel
        : el("span", { class: "badge" }, PROTO[chosen.protocol] || chosen.protocol));
    makeDropTarget(card, (files) => upload(targetOf(chosen), files, g.name),
                   () => sendPaths(targetOf(chosen), selection, g.name));
    box.append(card);
    g.targets.forEach((t) => select.append(el("option", { value: JSON.stringify(targetOf(t)) },
      `${g.name} (${PROTO[t.protocol] || t.protocol})`)));
  }
  if (!allGroups().length) {
    box.append(el("p", { class: "hint" }, "No devices yet – press “Search”. Phones must be unlocked with Quick Share/LocalSend open."));
  }
}

function targetOf(t) {
  return t.key ? { key: t.key } : { protocol: t.protocol, address: t.address, port: t.port };
}

// onSelection: what a click does while handed-over files are waiting.
function makeDropTarget(card, onFiles, onSelection) {
  card.addEventListener("dragover", (ev) => { ev.preventDefault(); card.classList.add("over"); });
  card.addEventListener("dragleave", () => card.classList.remove("over"));
  card.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    card.classList.remove("over");
    const files = await filesFromDrop(ev.dataTransfer);
    if (files.length) onFiles(files);
  });
  card.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-stop], label")) return;   // controls inside the card
    if (selection.length && onSelection) { onSelection(); return; }
    const input = $("#pick-files");
    input.value = "";
    input.onchange = () => {
      const files = [...input.files].map((f) => ({ file: f, path: f.name }));
      if (files.length) onFiles(files);
    };
    input.click();
  });
}

// Folders arrive as directory entries; walk them to keep relative paths.
async function filesFromDrop(dt) {
  const entries = [...dt.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dt.files].map((f) => ({ file: f, path: f.name }));
  const out = [];
  async function walk(entry, prefix) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walk(e, `${prefix}${entry.name}/`);
      }
    }
  }
  for (const e of entries) await walk(e, "");
  return out;
}

async function upload(target, files, label, pin) {
  const form = new FormData();
  form.append("target", JSON.stringify(target));
  if (pin) form.append("pin", pin);
  for (const f of files) {
    form.append("path", f.path);
    form.append("file", f.file, f.file.name);
  }
  const total = files.reduce((s, f) => s + f.file.size, 0);
  addLog(`Handing over ${files.length} file(s), ${human(total)} to ${label} …`);
  try {
    const { job } = await api("/api/send", { method: "POST", body: form });
    remember(job, { target, files, label });
  } catch (e) {
    addLog(`Sending to ${label} failed: ${e.message}`);
  }
}

async function sendPaths(target, paths, label, pin) {
  addLog(`Handing over ${paths.length} item(s) to ${label} …`);
  try {
    const { job } = await api("/api/send", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, paths, pin: pin || null }) });
    remember(job, { target, paths, label });
    if (paths === selection) await clearSelection();
  } catch (e) {
    addLog(`Sending to ${label} failed: ${e.message}`);
  }
}

async function sendText(target, text, label, pin) {
  const { job } = await api("/api/send", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, text, pin: pin || null }) });
  remember(job, { target, text, label });
}

// Kept so a LocalSend receiver that asks for a PIN can be retried without
// picking the files again.
function remember(job, what) {
  if (isLocalSend(what.target)) resend.set(job, what);
  while (resend.size > 10) resend.delete(resend.keys().next().value);
  renderJobs();
}

function isLocalSend(t) {
  if (t.protocol) return t.protocol === "localsend";
  return (t.key || "").startsWith("localsend:");
}

function retryWithPin(id, pin) {
  const w = resend.get(id);
  if (!w || !pin) return;
  resend.delete(id);
  if (w.files) upload(w.target, w.files, w.label, pin);
  else if (w.paths) sendPaths(w.target, w.paths, w.label, pin);
  else sendText(w.target, w.text, w.label, pin).catch((e) => addLog(`Sending failed: ${e.message}`));
}

// -- browser download share ------------------------------------------------

async function startShare(files) {
  const form = new FormData();
  form.append("minutes", $("#share-minutes").value);
  if ($("#share-nopin").checked) form.append("no_pin", "1");
  if ($("#share-https").checked) form.append("https", "1");
  for (const f of files) {
    form.append("path", f.path);
    form.append("file", f.file, f.file.name);
  }
  try {
    showShare(await api("/api/share", { method: "POST", body: form }));
  } catch (e) {
    addLog(`Sharing failed: ${e.message}`);
  }
}

async function shareSelection() {
  try {
    showShare(await api("/api/share", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: selection, minutes: +$("#share-minutes").value,
                             no_pin: $("#share-nopin").checked,
                             https: $("#share-https").checked }) }));
    await clearSelection();
  } catch (e) {
    addLog(`Sharing failed: ${e.message}`);
  }
}

// -- files handed over by the desktop (Explorer "Send to") ------------------

async function loadSelection() {
  selection = (await api("/api/selection")).paths;
  $("#selection").replaceChildren(...selection.map((p) =>
    el("li", { title: p }, p.split(/[\\/]/).pop())));
  $("#selection-section").hidden = !selection.length;
}

async function clearSelection() {
  await api("/api/selection", { method: "DELETE" });
  await loadSelection();
}

function showShare(s) {
  const box = $("#share");
  box.hidden = !s || !s.running;
  if (box.hidden) return;
  const doc = new DOMParser().parseFromString(s.svg, "image/svg+xml");   // from our daemon
  $("#share-qr").replaceChildren(document.importNode(doc.documentElement, true));
  $("#share-url").textContent = s.url;
  $("#share-url").href = s.url;
  $("#share-pin-row").hidden = !s.pin;
  $("#share-pin").textContent = s.pin || "";
  $("#share-tls-hint").hidden = !s.url.startsWith("https:");
  // iPhone hotspots always use 172.20.10.0/28; the hotspot iPhone cannot reach its clients.
  $("#share-hotspot").hidden = !/^https?:\/\/172\.20\.10\./.test(s.url);
  const until = new Date(s.expires * 1000).toLocaleTimeString();
  $("#share-info").textContent =
    `${s.files.length} file(s), ${human(s.size)} · until ${until} · ${s.downloads} download(s)`;
}

// -- jobs -----------------------------------------------------------------

function renderJobs() {
  const box = $("#jobs");
  box.replaceChildren();
  const list = [...jobs.entries()].reverse().slice(0, 10);
  if (!list.length) box.append(el("p", { class: "hint" }, "Nothing sent yet."));
  for (const [id, j] of list) {
    let done = 0, size = 0;
    for (const f of j.files.values()) { done += f.done; size += f.size; }
    const label = { running: "running", waiting: "waiting for acceptance", done: "done",
                    failed: "failed", cancelled: "cancelled", qr: "waiting for scan" }[j.state] || j.state;
    const row = el("div", { class: `job card ${j.state}` },
      el("span", { class: "what" }, `${j.info.items.join(", ")} → ${j.info.to}`),
      size ? el("progress", { max: size, value: done }) : "",
      el("span", { class: "state" }, j.error ? `${label}: ${j.error}` : label));
    if (j.state === "failed" && j.pinRequired && resend.has(id)) {
      const input = el("input", { class: "pin-input", inputmode: "numeric", autocomplete: "off",
                                  placeholder: "PIN", "aria-label": "Receiver PIN" });
      row.append(el("form", { class: "row pin-form", onsubmit: (ev) => {
        ev.preventDefault();
        retryWithPin(id, input.value.trim());
      } }, input, el("button", { type: "submit", class: "primary" }, "Send again with PIN")));
    }
    if (["running", "waiting", "qr"].includes(j.state)) {
      row.append(el("button", { type: "button", onclick: () =>
        api(`/api/jobs/${id}/cancel`, { method: "POST" }).catch(() => {}) }, "Cancel"));
    }
    box.append(row);
  }
}

function onJobEvent(ev) {
  if (ev.event === "job_started") {
    jobs.set(ev.job, { info: ev.info, files: new Map(), state: "running" });
  }
  const j = jobs.get(ev.job);
  if (!j) return;
  switch (ev.event) {
    case "progress": j.files.set(ev.file, { done: ev.done, size: ev.size }); break;
    case "waiting": j.state = "waiting"; break;
    case "pin": j.pin = ev.pin; addLog(`Code ${ev.pin} for ${ev.to} – must match the receiver`); break;
    case "qr": j.state = "qr"; showQr(ev.svg); break;
    case "qr_scanned": j.state = "running"; j.info.to = ev.device; showQr(null); break;
    case "job_done": j.state = "done"; j.info = ev.info; showQr(null); break;
    case "job_failed":
      j.state = "failed"; j.error = ev.error; j.pinRequired = !!ev.pin_required; showQr(null); break;
    case "job_cancelled": j.state = "cancelled"; showQr(null); break;
  }
  if (j.state === "waiting" && ev.event === "progress") j.state = "running";
  renderJobs();
}

function showQr(svg) {
  const box = $("#qr");
  box.replaceChildren();
  if (!svg) return;
  // The SVG is generated by our own daemon (segno), not by a peer.
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  box.append(document.importNode(doc.documentElement, true));
}

// -- event stream and log -------------------------------------------------

const LOG_TEXT = {
  offer: (e) => `${e.offer.from} wants to send ${e.offer.files.length} item(s) (${PROTO[e.offer.protocol]})`,
  offer_accepted: (e) => `accepted${e.auto ? " (automatically)" : ""}` +
    (e.partial ? ` (${e.offer.files.length} of ${e.offer.offered})` : ""),
  offer_rejected: () => "declined",
  offer_expired: () => "request expired (no answer)",
  offer_withdrawn: () => "sender gave up",
  file_received: (e) => `received: ${e.file} → ${e.path}`,
  message: (e) => `${e.kind === "url" ? "Link" : "Text"} from ${e.from}: ${e.text}`,
  session_done: (e) => `received from ${e.from}`,
  failed: (e) => `error with ${e.from || "?"}: ${e.error}`,
  file_failed: (e) => `file failed: ${e.file} (${e.error})`,
  cancelled: (e) => `cancelled by ${e.from}`,
  rejected: (e) => `declined: ${e.from || e.to || ""} ${e.reason || ""}`,
  job_done: (e) => `sent to ${e.info.to}: ${e.info.items.join(", ")}`,
  job_failed: (e) => `sending to ${e.info.to} failed: ${e.error}` +
    (e.pin_required && !e.restored ? " – enter the PIN under “Transfers”" : ""),
  qr_scanned: (e) => `QR code scanned by ${e.device}`,
  share_started: (e) => `browser download started: ${e.files.length} file(s)`,
  share_visit: (e) => `browser download opened by ${e.from}`,
  share_download: (e) => `${e.from} is downloading ${e.file}`,
  share_wrong_pin: (e) => `wrong PIN from ${e.from}`,
  share_stopped: (e) => `browser download ended (${e.reason}, ${e.downloads} download(s))`,
  share_moved: (e) => `browser download moved to the new network: ${e.url}`,
};

function addLog(text, time, old) {
  const t = new Date((time || Date.now() / 1000) * 1000);
  const log = $("#log");
  const when = t.toDateString() === new Date().toDateString()
    ? t.toLocaleTimeString() : t.toLocaleString();
  log.prepend(el("li", old ? { class: "old", title: "before the last restart" } : {},
                 el("time", {}, when), text));
  while (log.children.length > 200) log.lastChild.remove();
}

function connect() {
  const es = new EventSource(`/api/events?t=${encodeURIComponent(token)}&since=${lastSeq}`);
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    lastSeq = Math.max(lastSeq, ev.seq);
    if (ev.job) onJobEvent(ev);
    else if (ev.event === "job_started") onJobEvent(ev);
    if (ev.event.startsWith("offer")) loadOffers().catch(() => {});
    if (ev.event === "selection") loadSelection().catch(() => {});
    if (["share_download", "share_stopped", "share_moved"].includes(ev.event)) {
      api("/api/share").then(showShare).catch(() => {});
    }
    const text = LOG_TEXT[ev.event];
    if (text) addLog(text(ev), ev.time, ev.restored);
  };
  es.onerror = () => {
    $("#status").textContent = "Lost connection to the service – retrying …";
    es.close();
    setTimeout(() => { connect(); loadStatus().catch(() => {}); loadOffers().catch(() => {}); }, 3000);
  };
}

// -- app window on Windows: window.pywebview.api (win_window.DesktopApi) -------

function showNetwork(n) {
  $("#network").hidden = n.ok;
}

async function initNetwork(d) {
  const refresh = () => d.network().then(showNetwork).catch(() => {});
  $("#network-allow").onclick = async () => {
    $("#network-status").textContent = "Waiting for Windows …";
    try {
      const n = await d.allow_network();
      showNetwork(n);
      $("#network-status").textContent = n.ok ? "" : "That did not work – Windows said no.";
    } catch (e) {
      $("#network-status").textContent = String(e.message || e);
    }
  };
  await refresh();
  // Another Wi-Fi, or the first-start prompt answered (each check runs PowerShell).
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
}

const desktop = () => window.pywebview && window.pywebview.api;

async function initDesktop() {
  const d = desktop();
  const s = await d.settings();
  $("#welcome").hidden = !s.welcome;
  $("#welcome-ok").onclick = () => { $("#welcome").hidden = true; d.welcome_done(); };
  $("#set-autostart").checked = s.autostart;
  $("#set-sendto").checked = s.sendto;
  for (const [id, fn] of [["#set-autostart", "set_autostart"], ["#set-sendto", "set_sendto"]]) {
    $(id).onchange = (ev) => d[fn](ev.target.checked).catch((e) => {
      ev.target.checked = !ev.target.checked;
      addLog(`Could not change the setting: ${e.message || e}`);
    });
  }
  $("#open-downloads").onclick = () => d.open_downloads();
  initNetwork(d);
  $("#desktop-settings").hidden = false;
  if (s.mirror) initMirror(d);
  if (!s.audio) return;
  $("#desktop-audio").hidden = false;
  const run = (p) => p.then(showSpeakers).catch((e) => {
    $("#speaker-status").textContent = String(e.message || e);
  });
  $("#speaker-play").onclick = () => {
    $("#speaker-status").textContent = "Connecting …";
    run(d.play_on($("#speaker").value));
  };
  $("#speaker-stop").onclick = () => run(d.stop_audio());
  $("#speaker-search").onclick = () => {
    $("#speaker-status").textContent = "Searching …";
    run(d.search_speakers());
  };
  $("#speaker-volume").onchange = (ev) => run(d.set_volume(ev.target.value / 100));
  const sp = await d.speakers();
  showSpeakers(sp);
  if (!sp.list.length) $("#speaker-search").click();
}

function showSpeakers(sp) {
  const sel = $("#speaker");
  const chosen = sp.current || sel.value;
  sel.replaceChildren(...sp.list.map((r) => el("option",
    r.supported ? { value: r.name } : { value: r.name, disabled: "" },
    r.supported ? r.name : `${r.name} (not supported)`)));
  if (chosen) sel.value = chosen;
  $("#speaker-volume").value = Math.round(sp.volume * 100);
  $("#speaker-stop").hidden = !sp.current;
  $("#speaker-play").disabled = !sp.list.some((r) => r.supported);
  $("#speaker-status").textContent = sp.current ? `Playing on ${sp.current}`
    : sp.list.length ? "" : "No speakers found. They must be on the same Wi-Fi.";
}

const STREAM_STATE = { streaming: "mirroring", connecting: "connecting …",
                       pin_required: "waiting for the code" };
let pinFor = null;                // IP of the TV whose code or password is asked for

function initMirror(d) {
  $("#desktop-mirror").hidden = false;
  const run = (p) => p.then(showScreens).catch((e) => {
    $("#mirror-status").textContent = String(e.message || e);
  });
  $("#mirror-start").onclick = () => {
    const opt = $("#screen").selectedOptions[0];
    if (!opt) return;
    $("#mirror-status").textContent = "Connecting …";
    run(d.mirror_to(opt.value, +opt.dataset.port || null));
  };
  $("#mirror-pin").onsubmit = (ev) => {
    ev.preventDefault();
    const code = $("#mirror-code").value;
    $("#mirror-code").value = "";
    run(d.mirror_pin(pinFor, code));
  };
  const poll = () => {
    if (document.visibilityState === "visible") run(d.screens());
  };
  poll();
  setInterval(poll, 2000);
}

function showScreens(m) {
  const sel = $("#screen");
  const chosen = sel.value;
  sel.replaceChildren(...m.screens.map((s) => el("option", { value: s.ip, "data-port": s.port || "" },
    s.model ? `${s.name} (${s.model})` : s.name)));
  if (chosen) sel.value = chosen;
  $("#mirror-start").disabled = !m.screens.length;
  $("#mirror-streams").replaceChildren(...m.streams.map((s) => el("li", {},
    `${s.device}: ${STREAM_STATE[s.state] || s.state} `,
    el("button", { type: "button", class: "danger", onclick: () => {
      window.pywebview.api.stop_mirror(s.ip).then(showScreens).catch(() => {});
    } }, "Stop"))));
  const waiting = m.streams.find((s) => s.state === "pin_required");
  pinFor = waiting ? waiting.ip : null;
  $("#mirror-pin").hidden = !waiting;
  if (waiting) {
    const password = waiting.credential === "password";
    $("#mirror-pin-label").textContent = password
      ? `Password of ${waiting.device}` : `Code shown on ${waiting.device}`;
    $("#mirror-code").type = password ? "password" : "text";
    $("#mirror-code").inputMode = password ? "text" : "numeric";
  }
  $("#mirror-status").textContent = m.error
    || (m.screens.length ? "" : "Looking for Apple TVs and AirPlay TVs on your Wi-Fi …");
}

function init() {
  const m = location.hash.match(/t=([^&]+)/);
  token = m ? decodeURIComponent(m[1]) : sessionStorage.getItem("multidrop-token") || "";
  if (!token) { $("#notoken").hidden = false; return; }
  sessionStorage.setItem("multidrop-token", token);
  history.replaceState(null, "", location.pathname);   // token out of the address bar

  $("#refresh").addEventListener("click", () => loadDevices(true));
  makeDropTarget($("#qr-card"), (files) => upload({ qr: true }, files, "QR code"),
                 () => sendPaths({ qr: true }, selection, "QR code"));
  makeDropTarget($("#share-card"), startShare, shareSelection);
  $("#selection-clear").addEventListener("click", () => clearSelection().catch(() => {}));
  $("#share-stop").addEventListener("click", () =>
    api("/api/share", { method: "DELETE" }).catch(() => {}).finally(() => showShare(null)));
  api("/api/share").then(showShare).catch(() => {});
  $("#manual").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const raw = $("#manual-address").value.trim();
    const m = raw.match(/^\[?([^\]]+?)\]?(?::(\d+))?$/);
    if (!m) return;
    const protocol = $("#manual-protocol").value;
    manual.push({ name: raw, type: null, targets: [{ protocol, address: m[1], port: m[2] ? +m[2] : null }] });
    renderDevices();
  });
  $("#text-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const target = $("#text-target").value;
    if (!target) { addLog("No device selected"); return; }
    const label = $("#text-target").selectedOptions[0].textContent;
    try {
      await sendText(JSON.parse(target), $("#text").value, label);
      $("#text").value = "";
    } catch (e) { addLog(`Sending text failed: ${e.message}`); }
  });

  loadStatus().catch((e) => { $("#status").textContent = `Error: ${e.message}`; });
  loadOffers().catch(() => {});
  loadSelection().catch(() => {});
  if (desktop()) initDesktop().catch(() => {});
  else window.addEventListener("pywebviewready", () => initDesktop().catch(() => {}), { once: true });
  loadDevices(false);
  renderJobs();
  connect();
}

init();
