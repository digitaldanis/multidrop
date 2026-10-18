// MultiDrop web build (GitHub Pages). The same screens as daemon/web/app.js,
// on AnEntrypoint/wireweave instead of the local daemon. Peer-supplied strings
// (device and file names, texts) are always inserted as text, never as HTML.
import { createWire, newPairCode, loadZone, canReceiveToFolder } from "./wire.js";
import qrcode from "qrcode-generator";

const $ = (sel) => document.querySelector(sel);
const LS_ALIAS = "multidrop-alias";
const jobs = new Map();        // job id -> {info, files: Map(name -> {done,size}), state, error}
const unchecked = new Map();   // offer id -> Set of file ids the user unticked
let offers = [];               // incoming offers, oldest first
let pendingPair = null;        // {name} while a joiner waits for approval (host side)
let devices = [];              // paired devices, online or not
let nearbyList = [];           // unpaired devices on this network, online
let deviceByPk = new Map();    // online devices by key, for the text sender
let wire = null;
let alias = "";
let folder = null;             // the receive folder handle, once chosen
let folderReady = false;       // true while the browser grants write access to it

// -- receive folder: chosen once, kept in IndexedDB ------------------------

const openDb = () => new Promise((res, rej) => {
  const req = indexedDB.open("multidrop", 1);
  req.onupgradeneeded = () => req.result.createObjectStore("kv");
  req.onsuccess = () => res(req.result);
  req.onerror = () => rej(req.error);
});

async function kv(mode, run) {
  const db = await openDb();
  return new Promise((res, rej) => {
    const tx = db.transaction("kv", mode);
    const req = run(tx.objectStore("kv"));
    tx.oncomplete = () => res(req.result);
    tx.onerror = () => rej(tx.error);
  });
}

// A test double or a browser without storable handles stays in memory only.
async function saveFolder(handle) {
  try { await kv("readwrite", (s) => s.put(handle, "receive")); } catch { /* memory only */ }
}

async function loadFolder() {
  try { return await kv("readonly", (s) => s.get("receive")) || null; } catch { return null; }
}

async function refreshFolder() {
  folder = folder || (await loadFolder());
  folderReady = Boolean(folder) && (await folder.queryPermission({ mode: "readwrite" })) === "granted";
  renderFolder();
}

// Picking happens inside the click, so the picker opens synchronously.
async function chooseFolder() {
  let handle;
  try {
    handle = await window.showDirectoryPicker({ mode: "readwrite", id: "multidrop-receive" });
  } catch {
    return;                                              // cancelled
  }
  folder = handle;
  folderReady = true;
  await saveFolder(handle);
  renderFolder();
  addLog(`Accepted files will be written into “${handle.name}”`);
}

async function allowFolder() {
  if (!folder) return;
  folderReady = (await folder.requestPermission({ mode: "readwrite" })) === "granted";
  renderFolder();
}

async function forgetFolder() {
  folder = null;
  folderReady = false;
  try { await kv("readwrite", (s) => s.delete("receive")); } catch { /* nothing stored */ }
  renderFolder();
}

function renderFolder() {
  const supported = canReceiveToFolder();
  $("#folder-note").hidden = supported;
  $("#folder-choose").disabled = !supported;
  $("#folder-state").textContent = !folder ? "Not set"
    : folderReady ? `Writing to “${folder.name}”`
    : `“${folder.name}” needs permission again`;
  $("#folder-allow").hidden = !(folder && !folderReady);
  $("#folder-forget").hidden = !folder;
}

// Paired devices write straight into the chosen folder; everyone else still asks.
const isPaired = (pk) => Boolean(loadZone()?.members?.[pk]);

function autoAccept(o) {
  if (!folderReady || !folder || !isPaired(o.pubkey)) return false;
  acceptOffer(o, o.files.map((f) => f.id), folder);
  return true;
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
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

function randomId(n, alphabet = "abcdefghjkmnpqrstuvwxyz23456789") {
  return [...crypto.getRandomValues(new Uint8Array(n))].map((b) => alphabet[b % alphabet.length]).join("");
}

function aliasName() {
  let name = localStorage.getItem(LS_ALIAS);
  if (!name) { name = `Browser ${randomId(4, "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")}`; localStorage.setItem(LS_ALIAS, name); }
  return name;
}

const spaced = (code) => `${code.slice(0, 3)} ${code.slice(3)}`;
const pageUrl = (params) => `${location.origin}${location.pathname}${params}`;

function showStatus(text) {
  $("#status").textContent = text;
  $("#status").classList.toggle("ready", text.startsWith("Ready"));
}

// -- pairing --------------------------------------------------------------

function showPairState(text) {
  $("#pair-state").textContent = text;
}

function renderPairRequest() {
  const box = $("#pair-requests");
  box.replaceChildren();
  if (!pendingPair) return;
  box.append(el("div", { class: "card offer" },
    el("div", { class: "name" }, `${pendingPair.name} wants to pair with this device`),
    el("div", { class: "meta" }, "Accept only a device you can see in front of you."),
    el("div", { class: "row" },
      el("button", { class: "primary", type: "button", onclick: () => answerPair(true) }, "Accept"),
      el("button", { class: "danger", type: "button", onclick: () => answerPair(false) }, "Decline"))));
}

function answerPair(accept) {
  pendingPair = null;
  renderPairRequest();
  wire.answerPair(accept);
  if (accept) showPairState("Pairing …");
}

function showHostCode() {
  const code = newPairCode();
  const url = pageUrl(`#pair=${code}`);
  $("#pair-code-show").textContent = spaced(code);
  $("#pair-url").textContent = url;
  $("#pair-url").href = url;
  const qr = qrcode(0, "M");
  qr.addData(url);
  qr.make();
  const svg = new DOMParser().parseFromString(qr.createSvgTag(4, 2), "image/svg+xml");
  $("#pair-qr").replaceChildren(document.importNode(svg.documentElement, true));
  $("#pair-host").hidden = false;
  showPairState("Waiting for the other device to enter this code …");
  wire.openPairing(code, "host");
}

function hideHostCode() {
  $("#pair-host").hidden = true;
  wire.closePairing();
}

function joinWithCode(raw) {
  const code = String(raw).replace(/\D/g, "");
  if (code.length !== 6) { showPairState("Enter the 6-digit code shown on the other device."); return; }
  showPairState("Connecting …");
  wire.openPairing(code, "joiner");
}

function renderZoneState() {
  const zone = loadZone();
  $("#leave-zone").hidden = !zone;
  if ($("#pair-state").textContent) return;
  showPairState(zone
    ? `Paired${zone.host ? ` via ${zone.host}` : ""}. This device stays in your zone.`
    : "No remembered devices. Devices on this Wi-Fi appear without this.");
}

// -- status, offers -------------------------------------------------------

function renderOffers() {
  const box = $("#offers");
  box.replaceChildren();
  $("#offers-section").hidden = offers.length === 0;
  for (const o of offers) {
    const selectable = o.files.length > 1;
    const off = unchecked.get(o.id) || new Set();
    unchecked.set(o.id, off);
    const items = el("ul", { class: selectable ? "files selectable" : "files" });
    for (const f of o.files) {
      const label = `${f.name} (${human(f.size)})`;
      if (!selectable) { items.append(el("li", {}, label)); continue; }
      const cb = el("input", { type: "checkbox", "data-file": f.id, onchange: (ev) => {
        if (ev.target.checked) off.delete(f.id); else off.add(f.id);
      } });
      cb.checked = !off.has(f.id);
      items.append(el("li", {}, el("label", {}, cb, " ", label)));
    }
    const answer = (accept, ids) => acceptOffer(o, accept ? ids : []);
    const everyFile = o.files.map((f) => f.id);
    const chosen = () => everyFile.filter((id) => !off.has(id));
    box.append(el("div", { class: "card offer" },
      el("div", { class: "name" }, `${o.from} wants to send`),
      el("div", { class: "meta" }, `${o.via === "relay" ? "Via encrypted relays" : "Browser (WebRTC)"} · ${human(o.total_size)}`),
      items,
      el("div", { class: "row" },
        el("button", { class: "primary", type: "button", disabled: canReceiveToFolder() ? undefined : "",
           onclick: () => answer(true, everyFile) }, selectable ? "Accept all" : "Accept"),
        selectable ? el("button", { type: "button", "data-action": "accept-selected",
          disabled: canReceiveToFolder() ? undefined : "", onclick: () => {
          const ids = chosen();
          if (ids.length) answer(true, ids); else answer(false);
        } }, "Accept selected") : "",
        el("button", { class: "danger", type: "button", onclick: () => answer(false) }, "Decline")),
      canReceiveToFolder() ? "" : el("p", { class: "hint" },
        "Receiving needs Chrome, Edge or Opera: files are written straight to a folder you choose.")));
  }
}

// Accepting picks the folder first: the picker must open inside the click, so the
// call starts synchronously. Files then stream into that folder, never into memory.
async function acceptOffer(o, ids, known = null) {
  let dir = known || (folderReady ? folder : null);
  if (!dir && ids.length) {
    try {
      dir = await window.showDirectoryPicker({ mode: "readwrite", id: "multidrop" });
    } catch {
      dir = null;                                         // picker cancelled
    }
  }
  wire.answer(o.id, Boolean(dir), ids, dir);
  offers = offers.filter((x) => x.id !== o.id);
  unchecked.delete(o.id);
  renderOffers();
  if (dir) addLog(`Saving ${ids.length} file(s) from ${o.from} into “${dir.name}”`);
}

// -- devices and sending --------------------------------------------------

function refreshDevices() {
  devices = wire ? wire.members() : [];
  nearbyList = wire ? wire.nearby() : [];
  renderDevices();
}

function renderNetwork() {
  const state = wire ? wire.network() : "checking";
  $("#nearby-hint").textContent = {
    checking: "Looking for devices on this network …",
    on: "",
    unavailable: "Nearby is off: this browser cannot read its network address, so only remembered devices appear.",
  }[state];
}

function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function hueOf(text) {
  let h = 0;
  for (const ch of text) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

// Rings around "you": up to six on the inner ring, the rest on the outer one.
function ringPosition(i, total) {
  const ring = i < 6 ? 0 : 1;
  const count = ring ? total - 6 : Math.min(total, 6);
  const index = ring ? i - 6 : i;
  return { ring, angle: (index * 360) / count + (ring ? 30 : 0) };
}

// Where a device sits on the radar: paired devices, and nearby ones that share the
// Wi-Fi or are close by, sit on the inner ring; the rest on the outer one.
function layout(items) {
  const inner = items.filter((d) => !d.nearby || d.lan || (d.rtt != null && d.rtt < 20));
  const outer = items.filter((d) => !inner.includes(d));
  const at = new Map();
  inner.forEach((d, i) => at.set(d, { ring: 0, angle: (i * 360) / inner.length }));
  outer.forEach((d, i) => at.set(d, { ring: 1, angle: (i * 360) / outer.length + 30 }));
  return at;
}

function reachLabel(d) {
  if (!d.online) return `Last seen ${ago(d.seen)}`;
  if (!d.nearby) return d.reach === "relay" ? "Via relays" : "Available";
  if (d.reach === "relay") return "Via relays";
  if (d.lan) return d.rtt != null ? `On your Wi-Fi · ${d.rtt} ms` : "On your Wi-Fi";
  return d.rtt != null ? `Same network · ${d.rtt} ms` : "Same network";
}

function renderDevices() {
  const box = $("#devices");
  box.replaceChildren();
  const select = $("#text-target");
  const previous = select.value;
  select.replaceChildren();
  const paired = loadZone();
  box.append(el("div", { class: "me" },
    el("div", { class: "avatar" }, (alias[0] || "?").toUpperCase()),
    el("div", { class: "name" }, alias),
    el("div", { class: "meta" }, paired ? "Visible on this Wi-Fi and to your devices" : "Visible on this Wi-Fi")));

  const items = [
    ...devices.map((d) => ({ ...d, nearby: false })),
    ...nearbyList.map((d) => ({ ...d, nearby: true })),
  ];
  const sendable = items.filter((d) => d.online);
  deviceByPk = new Map(sendable.map((d) => [d.pubkey, d]));

  // The text target lists only devices that are online right now; with none,
  // the control is disabled and says why, instead of showing an empty dropdown.
  select.disabled = sendable.length === 0;
  $("#text-form button[type=submit]").disabled = sendable.length === 0;
  if (!sendable.length) select.append(el("option", { value: "" }, "No device nearby yet"));
  for (const d of sendable) select.append(el("option", { value: d.pubkey }, d.reach === "relay" ? `${d.name} (via relays)` : d.name));

  const add = { pubkey: "", add: true, nearby: false };
  const all = [...items, add];
  box.classList.toggle("big", all.length > 6);
  const at = layout(all);
  for (const d of all) {
    const spot = at.get(d);
    const node = d.add
      ? el("div", { class: "device pairbubble", title: "Remember a device on other networks" },
          el("div", { class: "avatar" }, "+"), el("div", { class: "name" }, "Remember a device"))
      : el("div", { class: [
            d.online ? "device card drop" : "device offline",
            d.nearby ? "nearby" : "",
            d.reach === "relay" ? "relay" : "",
          ].join(" ").replace(/\s+/g, " ").trim() },
          el("div", { class: "avatar", style: `--hue:${hueOf(d.pubkey)}` }, (d.name[0] || "?").toUpperCase()),
          el("div", { class: "name" }, d.name),
          el("div", { class: "meta" }, reachLabel(d)));
    if (d.add) {
      node.addEventListener("click", () => {
        $("#pair-section").open = true;
        $("#pair-section").scrollIntoView({ behavior: "smooth" });
        $("#pair-code").focus();
      });
    } else if (d.online) {
      makeDropTarget(node, (files) => sendTo(d, files));
    } else {
      node.title = "Offline. It appears here as soon as its page is open.";
    }
    node.setAttribute("style", `--a:${spot.angle}deg;--r:var(--r${spot.ring})`);
    box.append(node);
  }
  if (previous && deviceByPk.has(previous)) select.value = previous;
  renderNetwork();
}

// Direct when the device is reachable on the local link; otherwise the user is asked
// before anything goes through the encrypted relays.
function sendTo(d, files) {
  if (d.reach !== "relay") return sendFiles(d, files, "direct");
  const total = files.reduce((sum, f) => sum + f.file.size, 0);
  const ok = window.confirm(
    `${d.name} cannot be reached directly right now.\n\n` +
    `Send ${files.length} file(s), ${human(total)}, through encrypted relays instead? ` +
    "It is slower, and the relays see when and how much is sent, never the contents.");
  if (ok) sendFiles(d, files, "relay");
}

function makeDropTarget(card, onFiles) {
  card.addEventListener("dragover", (ev) => { ev.preventDefault(); card.classList.add("over"); });
  card.addEventListener("dragleave", () => card.classList.remove("over"));
  card.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    card.classList.remove("over");
    const files = await filesFromDrop(ev.dataTransfer);
    if (files.length) onFiles(files);
  });
  card.addEventListener("click", () => {
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

function sendFiles(device, files, via) {
  const total = files.reduce((s, f) => s + f.file.size, 0);
  addLog(`Handing over ${files.length} file(s), ${human(total)} to ${device.name}${via === "relay" ? " via encrypted relays" : ""} …`);
  try {
    wire.sendFiles(device.pubkey, files, device.name, via);
  } catch (e) {
    addLog(`Sending to ${device.name} failed: ${e.message}`);
  }
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
                    failed: "failed", cancelled: "cancelled" }[j.state] || j.state;
    const row = el("div", { class: `job card ${j.state}` },
      el("span", { class: "what" }, `${j.info.items.join(", ")} → ${j.info.to}`),
      size ? el("progress", { max: size, value: done }) : "",
      el("span", { class: "state" }, j.error ? `${label}: ${j.error}` : label));
    if (["running", "waiting"].includes(j.state)) {
      row.append(el("button", { type: "button", onclick: () => wire.cancel(id) }, "Cancel"));
    }
    box.append(row);
  }
}

function onJobEvent(ev) {
  if (ev.event === "job_started") {
    jobs.set(ev.job, { info: ev.info, files: new Map(), state: "waiting" });
    if (jobs.size > 50) jobs.delete(jobs.keys().next().value);
  }
  const j = jobs.get(ev.job);
  if (!j) return;
  switch (ev.event) {
    case "progress": j.state = "running"; j.files.set(ev.file, { done: ev.done, size: ev.size }); break;
    case "job_done": j.state = "done"; break;
    case "job_failed": j.state = "failed"; j.error = ev.error; break;
    case "job_cancelled": j.state = "cancelled"; break;
  }
  renderJobs();
}

// -- events and history ---------------------------------------------------

function addLog(content) {
  const log = $("#log");
  log.prepend(el("li", {}, el("time", {}, new Date().toLocaleTimeString()), content));
  while (log.children.length > 200) log.lastChild.remove();
}

function onEvent(ev) {
  switch (ev.event) {
    case "connected":
      showStatus(`Ready – visible as “${alias}”`);
      break;
    case "devices":
      refreshDevices();
      renderZoneState();
      break;
    case "network":
      renderNetwork();
      refreshDevices();
      break;
    case "pair_waiting":
      showPairState(`Connected to ${ev.name}. Waiting for it to accept …`);
      break;
    case "pair_request":
      pendingPair = { name: ev.name };
      $("#pair-section").open = true;
      renderPairRequest();
      addLog(`${ev.name} asked to pair`);
      break;
    case "paired":
      $("#pair-code").value = "";
      showPairState(`Paired with ${ev.name}. This device stays in your zone.`);
      renderZoneState();
      addLog(`Paired with ${ev.name}`);
      break;
    case "pair_done":
      hideHostCode();
      showPairState(`Paired with ${ev.name || "the other device"}. Devices stay in your zone.`);
      renderZoneState();
      addLog(`Paired with ${ev.name || "a device"}`);
      break;
    case "pair_declined":
      hideHostCode();
      pendingPair = null;
      renderPairRequest();
      showPairState("Pairing declined.");
      break;
    case "pair_expired":
      hideHostCode();
      showPairState("The code expired. Show a new one to pair.");
      break;
    case "pair_lost":
      showPairState("Connection to the other device was lost. Try again.");
      break;
    case "offer":
      if (autoAccept(ev.offer)) {
        addLog(`${ev.offer.from} sent ${ev.offer.files.length} item(s); writing to “${folder.name}”`);
        break;
      }
      offers.push(ev.offer);
      renderOffers();
      addLog(`${ev.offer.from} wants to send ${ev.offer.files.length} item(s)`);
      break;
    case "offer_withdrawn":
      offers = offers.filter((o) => o.id !== ev.id);
      renderOffers();
      break;
    case "file_saved":
      addLog(`saved ${ev.file} from ${ev.from} (${human(ev.size)})`);
      break;
    case "message":
      addLog(`${ev.kind === "url" ? "Link" : "Text"} from ${ev.from}: ${ev.text}`);
      break;
    case "job_started":
    case "progress":
    case "job_done":
    case "job_failed":
    case "job_cancelled":
      onJobEvent(ev);
      if (ev.event === "job_failed") addLog(`sending failed: ${ev.error}`);
      break;
    case "error":
      addLog(`Error: ${ev.message}`);
      break;
  }
}

// -- init -----------------------------------------------------------------

function init() {
  alias = aliasName();
  document.querySelectorAll(".alias").forEach((e) => { e.textContent = `“${alias}”`; });
  showStatus("connecting …");

  const nameInput = $("#alias-input");
  nameInput.value = alias;
  nameInput.addEventListener("change", () => {
    const name = nameInput.value.trim().slice(0, 40);
    if (!name) { nameInput.value = alias; return; }
    alias = name;
    localStorage.setItem(LS_ALIAS, name);
    showStatus(`Ready – visible as “${alias}”`);
    wire.setAlias(name);
    refreshDevices();
  });
  setInterval(refreshDevices, 60000);                     // keeps "last seen" fresh
  $("#pair-show").addEventListener("click", showHostCode);
  $("#folder-choose").addEventListener("click", chooseFolder);
  $("#folder-allow").addEventListener("click", allowFolder);
  $("#folder-forget").addEventListener("click", forgetFolder);
  refreshFolder();
  $("#pair-cancel").addEventListener("click", () => { hideHostCode(); showPairState(""); renderZoneState(); });
  $("#pair-join").addEventListener("submit", (ev) => {
    ev.preventDefault();
    joinWithCode($("#pair-code").value);
  });
  $("#leave-zone").addEventListener("click", () => {
    wire.leaveZone();
    addLog("This device left its zone");
    renderZoneState();
    refreshDevices();
  });
  $("#text-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const pk = $("#text-target").value;
    if (!pk) { addLog("No device selected"); return; }
    const select = $("#text-target");
    const label = select.selectedOptions[0].textContent;
    const text = $("#text").value.trim();
    if (!text) return;
    const target = deviceByPk.get(pk);
    wire.sendText(pk, text, label, target?.reach === "relay" ? "relay" : "direct");
    $("#text").value = "";
  });

  renderOffers();
  renderJobs();

  wire = createWire({ alias, onEvent });
  if (new URLSearchParams(location.search).has("debug")) window.multidrop = { wire };   // test hook
  window.addEventListener("pagehide", () => { wire.leave(); });

  // Keys must exist before any room is joined, so join first; a pairing link (QR code)
  // then prefills the code and starts pairing at once.
  const link = location.hash.match(/pair=(\d{6})/);
  if (link) history.replaceState(null, "", location.pathname);
  wire.join()
    .then(() => {
      showStatus(`Ready – visible as “${alias}”`);
      refreshDevices();
      renderZoneState();
      if (link) {
        $("#pair-section").open = true;
        $("#pair-code").value = link[1];
        joinWithCode(link[1]);
      }
    })
    .catch((e) => showStatus(`Could not join: ${e.message}`));
}

init();
