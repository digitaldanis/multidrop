// Browser transport for the MultiDrop web app and its desktop launcher.
// AnEntrypoint/wireweave carries presence and WebRTC signalling over nostr relays.
//
// Three ways a file can move, chosen per device:
//   direct  - WebRTC data channel (ordered; chunks are acknowledged in a window).
//   relay   - NIP-59 / NIP-44 encrypted messages through the same nostr relays. Used
//             only when the user agrees, for a device that is online but cannot be
//             reached directly. Relays may reorder or drop messages, so chunks carry
//             sequence numbers, the receiver confirms each file, and the sender
//             resends what is unconfirmed.
//
// Two links find devices. `zone` holds paired devices (a shared secret). `nearby`
// holds devices behind the same public address (ICE learns it from a STUN server);
// of those, only devices that pass the local-network probe are shown or accepted,
// so strangers behind the same carrier NAT or campus address stay invisible.
//
// Privacy: presence events on the public relays carry only a neutral name; real
// device names travel over the encrypted data channel. No shared TURN servers.
//
// Proximity: a direct probe over the nearby link tells whether two devices reach
// each other on the local network (no public address involved), and the data
// channel's round-trip time gives a distance estimate.
//
// Pairing: a 6-digit code names a short-lived pairing room. The host approves the
// joiner, then sends a zone secret over the encrypted data channel.
import { createWireweave } from "./vendor/wireweave/src/wireweave.js";
import { createDataSession, setIceServers } from "./vendor/wireweave/src/data.js";
import * as NostrTools from "nostr-tools";
import * as xstate from "xstate";

const NAMESPACE = "multidrop";
const RELAYS = [
  "wss://relay.primal.net",
  "wss://nos.lol",
  "wss://offchain.pub",
  "wss://nostr-pub.wellorder.net",
  "wss://relay.snort.social",
];
const STUN = [{ urls: "stun:stun.l.google.com:19302" }, { urls: "stun:stun.cloudflare.com:3478" }];
const ANON = "MultiDrop";                // the name relays see in presence events
setIceServers(STUN);                     // instead of wireweave's shared public TURN credentials
const ZONE_KEY = "multidrop-zone";
const CHUNK = 64 * 1024;                 // direct chunk; well under the message limit browsers accept
const WINDOW = 8 * CHUNK;                // unacknowledged bytes a direct sender may have in flight
const MAX_BUFFERED = 4 * 1024 * 1024;    // pause direct sending while the channel is this full
const RELAY_CHUNK = 24 * 1024;           // relay chunk: base64 inside NIP-44 keeps the event well under relay limits
const RELAY_WINDOW = 8 * RELAY_CHUNK;
const RELAY_STALL_MS = 8000;             // no progress this long: resend what is unconfirmed
const RELAY_GIVE_UP_MS = 60000;          // no progress this long: the transfer fails
const RELAY_FRESH_S = 600;               // relays hand back old messages; ignore anything older
const PAIR_TTL_MS = 5 * 60 * 1000;       // a shown code stops working after this
const PAIR_ACK_MS = 5000;                // host waits this long for the joiner's ack
const STALE_MS = 30000;                  // a peer that has not opened by then is dropped
const SEEN_MS = 90000;                   // a device announced this recently counts as online
const SAMPLE_MS = 5000;                  // distance and presence sampling interval
const NETWORK_CHECK_MS = 120000;         // re-read the network address this often
const MEMBER_FRESH_MS = 60000;           // do not rewrite a member's last-seen more often than this
const LAN_TIMEOUT_MS = 8000;             // local-network probe gives up after this
const LAN_FRESH_MS = 600000;             // re-probe a device's local-network reach this often

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const newId = () => crypto.randomUUID().slice(0, 12);
const baseName = (p) => p.split(/[\\/]/).filter(Boolean).pop() || "file";
const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
const sha256Hex = async (text) =>
  hex(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))));
const zoneRoom = (secret) => sha256Hex(`zone:${secret}`);
const nearbyRoom = (networkKey) => sha256Hex(`net:${networkKey}`);
const pairRoom = (code) => sha256Hex(`pair:${code}`);
const newZoneSecret = () => hex(crypto.getRandomValues(new Uint8Array(16)));
const toB64 = (buf) => {
  const bytes = new Uint8Array(buf);
  let str = "";
  for (let i = 0; i < bytes.length; i += 8192) str += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(str);
};
const fromB64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0)).buffer;

export const newPairCode = () => String(100000 + (crypto.getRandomValues(new Uint32Array(1))[0] % 900000));

// Receiving streams straight to a folder the user picks (File System Access API);
// nothing is held in memory or stored on the page. Chromium-based browsers only.
export const canReceiveToFolder = () => typeof window.showDirectoryPicker === "function";

// A name that is free in the folder: "photo.jpg", then "photo (1).jpg", and so on.
async function freeFileHandle(dir, name) {
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  for (let i = 0; ; i++) {
    const candidate = i ? `${stem} (${i})${ext}` : name;
    try {
      await dir.getFileHandle(candidate);
    } catch (e) {
      if (e.name === "NotFoundError") return dir.getFileHandle(candidate, { create: true });
      throw e;
    }
  }
}

export function loadZone() {
  try { return JSON.parse(localStorage.getItem(ZONE_KEY)); } catch { return null; }
}

function saveZone(zone) {
  if (zone) localStorage.setItem(ZONE_KEY, JSON.stringify(zone));
  else localStorage.removeItem(ZONE_KEY);
}

// Paired devices are remembered with the zone, so they stay listed while offline.
function rememberMember(pk, name) {
  const zone = loadZone();
  if (!zone) return false;
  const members = zone.members || {};
  const now = Date.now();
  if (name === pk.slice(0, 8)) return false;              // wireweave's placeholder, not a real name
  const known = members[pk];
  if (known && known.name === name && now - known.seen < MEMBER_FRESH_MS) return false;
  members[pk] = { name, seen: now };
  saveZone({ ...zone, members });
  return true;
}

// The public address this network shows the internet, as a STUN server reports it.
// Devices on the same router report the same address. Null when STUN cannot be reached.
// `?network=` overrides it for testing two devices on one machine.
async function networkKey() {
  const forced = new URLSearchParams(location.search).get("network");
  if (forced) return forced;
  if (typeof RTCPeerConnection === "undefined") return null;
  return new Promise((resolve) => {
    const pc = new RTCPeerConnection({ iceServers: STUN });
    let found = null;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      pc.close();
      resolve(found);
    };
    pc.onicecandidate = (e) => {
      if (!e.candidate) return finish();
      if (e.candidate.type === "srflx" && !found) {
        found = e.candidate.address || e.candidate.candidate.split(" ")[4];
      }
    };
    pc.createDataChannel("probe");
    pc.createOffer().then((o) => pc.setLocalDescription(o)).catch(finish);
    setTimeout(finish, 4000);
  });
}

// Resolves once the peer connection has gathered its candidates (or after a bound).
function gathered(pc) {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") resolve();
    });
    setTimeout(resolve, 3000);
  });
}

export function createWire({ alias, onEvent }) {
  // Relays that store and return kind-30078 events reliably (checked per relay);
  // the wireweave default list includes several that drop them.
  const ww = createWireweave({ nostrTools: NostrTools, xstate, relays: RELAYS });
  const zone = ww.ensureData({ namespace: NAMESPACE });
  const nearby = createDataSession({ fsm: ww.fsm, xstate, relayPool: ww.pool, auth: ww.auth, namespace: NAMESPACE });
  const dm = ww.ensureDM();
  const links = [[zone, "zone"], [nearby, "nearby"]];
  const emit = (ev) => onEvent(ev);

  const outgoing = new Map();        // job id -> {id, pubkey, info, files, state, acked, via}
  const incoming = new Map();        // offer id -> {pubkey, files, via}, waiting for our answer
  const accepted = new Map();        // offer id -> {pubkey, dir, fileIds}, answered yes
  const receiving = new Map();       // pubkey -> file being written
  const queues = new Map();          // pubkey -> tail of that peer's transfer queue
  const cancelled = new Set();
  const names = new Map();           // pubkey -> name the device sent over the data channel
  const routes = new Map();          // offer/job id -> "direct" | "relay"
  const flags = new Set();           // "ready:<id>:<fid>", "done:<id>:<fid>" from the receiver
  const finishedFiles = new Set();   // "<id>:<fid>" written and closed here, so a repeated end still gets its done
  const lan = new Map();             // pubkey -> {state: pending|yes|no, at}
  const lanPcs = new Map();          // pubkey -> {pc, timer} while probing
  const seenVia = new Map();         // pubkey -> {link, at}, last announcement seen
  const rtts = new Map();            // pubkey -> round-trip time in ms on the direct link
  let pairing = null;                // {session, role, peer, name, ttl, ack}
  let networkKeyValue = null;
  let networkState = "checking";     // checking | on | unavailable
  let lastSignature = "";

  // The direct link a device is reachable on right now (zone first, then nearby).
  const linkOf = (pk) => links.map(([s]) => s).find((s) => s.peers.get(pk)?.dc?.readyState === "open") || null;
  const dcOf = (pk) => linkOf(pk)?.peers.get(pk)?.dc || null;
  const nameOf = (pk) =>
    names.get(pk)
    || links.map(([s]) => s.participants.get("nostr-" + pk.slice(0, 12))?.identity).find((n) => n && n !== ANON)
    || loadZone()?.members?.[pk]?.name
    || pk.slice(0, 8);
  const isMember = (pk) => Boolean(loadZone()?.members?.[pk]);
  // Messages are accepted from paired devices and from devices on the same local network.
  const onLanNow = (pk) => lan.get(pk)?.state === "yes";
  const knownPeer = (pk) => isMember(pk) || onLanNow(pk);

  const sendControl = (pk, msg) => {
    const s = linkOf(pk);
    if (!s || !s.send(pk, JSON.stringify(msg))) throw new Error("device is not connected");
  };
  const sendRaw = (pk, buf) => {
    const s = linkOf(pk);
    if (!s || !s.send(pk, buf)) throw new Error("device disconnected");
  };
  const relaySend = (pk, msg) => dm.send(pk, JSON.stringify(msg)).catch((e) => {
    throw new Error(`relay: ${e.message || e}`);
  });
  const sendVia = (via, pk, msg) => (via === "relay" ? relaySend(pk, msg) : Promise.resolve(sendControl(pk, msg)));
  // Replies follow the route of the offer or job they belong to.
  const reply = (id, pk, msg) => {
    const p = sendVia(routes.get(id) || "direct", pk, msg);
    p.catch(() => {});
    return p;
  };
  const announceName = (pk) => {
    try { sendControl(pk, { k: "name", name: alias }); } catch { /* the peer is gone; it asks again on reconnect */ }
  };

  const assertOpen = (pk) => {
    if (!dcOf(pk)) throw new Error("device disconnected");
  };

  const drain = async (pk) => {
    for (;;) {
      const dc = dcOf(pk);
      if (!dc) throw new Error("device disconnected");
      if (dc.bufferedAmount <= MAX_BUFFERED) return;
      await sleep(25);
    }
  };

  const enqueue = (pk, task) => {
    const next = (queues.get(pk) || Promise.resolve()).then(task);
    queues.set(pk, next.catch(() => {}));
    return next;
  };

  const key = (id, fid) => `${id}:${fid}`;

  // Direct transfer: ordered channel, acknowledged in a window.
  async function transfer(job, files) {
    job.state = "running";
    try {
      for (const f of files) {
        sendControl(job.pubkey, { k: "file", id: job.id, fid: f.fid, name: f.name, size: f.file.size });
        let sent = 0;
        for (let off = 0; off < f.file.size; off += CHUNK) {
          if (cancelled.has(job.id)) throw new Error("cancelled");
          await drain(job.pubkey);
          // Flow control: stop while too much is unacknowledged, so a slow disk
          // on the receiving side never has to buffer the rest of the file.
          while (sent - (job.acked.get(f.fid) || 0) >= WINDOW) {
            if (cancelled.has(job.id)) throw new Error("cancelled");
            assertOpen(job.pubkey);
            await sleep(10);
          }
          const buf = await f.file.slice(off, off + CHUNK).arrayBuffer();
          sendRaw(job.pubkey, buf);
          sent += buf.byteLength;
          emit({ event: "progress", job: job.id, file: f.name, done: sent, size: f.file.size });
        }
        sendControl(job.pubkey, { k: "end", id: job.id, fid: f.fid });
      }
      emit({ event: "job_done", job: job.id, info: job.info });
    } catch (e) {
      if (cancelled.has(job.id)) emit({ event: "job_cancelled", job: job.id });
      else emit({ event: "job_failed", job: job.id, error: e.message });
    } finally {
      finishJob(job);
    }
  }

  function finishJob(job) {
    outgoing.delete(job.id);
    cancelled.delete(job.id);
    for (const k of [...flags]) if (k.includes(job.id)) flags.delete(k);
  }

  // Resends `send` until `ok()` holds, every `every` ms, for at most `limit` ms.
  async function retryUntil(send, ok, every, limit, job) {
    const start = Date.now();
    for (;;) {
      if (ok()) return;
      if (job && cancelled.has(job.id)) throw new Error("cancelled");
      if (Date.now() - start > limit) throw new Error("no answer from the other device over relays");
      await send();
      for (let waited = 0; waited < every && !ok(); waited += 200) await sleep(200);
    }
  }

  // Relay transfer: each chunk is numbered; a stalled window is resent until it
  // moves again or gives up, and the receiver confirms every file.
  async function transferRelay(job, files) {
    job.state = "running";
    try {
      for (const f of files) {
        const header = { k: "file", id: job.id, fid: f.fid, name: f.name, size: f.file.size };
        await retryUntil(() => relaySend(job.pubkey, header), () => flags.has(`ready:${key(job.id, f.fid)}`), 2500, 30000, job);
        const sent = new Map();                       // seq -> message, kept until confirmed
        let sentBytes = 0;
        for (let seq = 0; seq * RELAY_CHUNK < f.file.size; seq++) {
          if (cancelled.has(job.id)) throw new Error("cancelled");
          await relayWindow(job, f, sent, sentBytes);
          const buf = await f.file.slice(seq * RELAY_CHUNK, (seq + 1) * RELAY_CHUNK).arrayBuffer();
          const msg = { k: "rc", id: job.id, fid: f.fid, seq, d: toB64(buf) };
          sent.set(seq, msg);
          await relaySend(job.pubkey, msg);
          sentBytes += buf.byteLength;
          emit({ event: "progress", job: job.id, file: f.name, done: sentBytes, size: f.file.size });
        }
        const end = { k: "end", id: job.id, fid: f.fid };
        await retryUntil(() => relaySend(job.pubkey, end), () => flags.has(`done:${key(job.id, f.fid)}`), 2500, RELAY_GIVE_UP_MS, job);
      }
      emit({ event: "job_done", job: job.id, info: job.info });
    } catch (e) {
      if (cancelled.has(job.id)) emit({ event: "job_cancelled", job: job.id });
      else emit({ event: "job_failed", job: job.id, error: e.message });
    } finally {
      finishJob(job);
    }
  }

  // Waits until the relay window has room, resending the unconfirmed chunks on stalls.
  async function relayWindow(job, f, sent, sentBytes) {
    let lastAcked = -1;
    let stalledSince = Date.now();
    for (;;) {
      const acked = job.acked.get(f.fid) || 0;
      for (const seq of [...sent.keys()]) if ((seq + 1) * RELAY_CHUNK <= acked) sent.delete(seq);
      if (sentBytes - acked < RELAY_WINDOW) return;
      if (cancelled.has(job.id)) throw new Error("cancelled");
      if (acked !== lastAcked) {
        lastAcked = acked;
        stalledSince = Date.now();
      } else if (Date.now() - stalledSince > RELAY_STALL_MS) {
        if (Date.now() - stalledSince > RELAY_GIVE_UP_MS) throw new Error("the relays stopped delivering");
        for (const msg of sent.values()) relaySend(job.pubkey, msg).catch(() => {});
        stalledSince = Date.now();
      }
      await sleep(200);
    }
  }

  function onControl(pk, msg, via) {
    switch (msg.k) {
      case "offer": {
        if (!knownPeer(pk)) return;                       // strangers are ignored
        routes.set(msg.id, via);
        incoming.set(msg.id, { pubkey: pk, files: msg.files, via });
        const files = msg.files.map((f) => ({ id: f.id, name: baseName(f.name), size: f.size }));
        emit({ event: "offer", offer: {
          id: msg.id, from: msg.from || nameOf(pk), pubkey: pk, files, via,
          total_size: files.reduce((s, f) => s + f.size, 0),
        } });
        return;
      }
      case "accept": {
        const job = outgoing.get(msg.id);
        if (!job || job.pubkey !== pk) return;
        const chosen = job.files.filter((f) => msg.files.includes(f.fid));
        if (!chosen.length) { outgoing.delete(job.id); emit({ event: "job_failed", job: job.id, error: "nothing accepted" }); return; }
        enqueue(pk, () => (job.via === "relay" ? transferRelay(job, chosen) : transfer(job, chosen)));
        return;
      }
      case "decline": {
        const job = outgoing.get(msg.id);
        if (!job || job.pubkey !== pk) return;
        outgoing.delete(msg.id);
        emit({ event: "job_failed", job: msg.id, error: "declined by receiver" });
        return;
      }
      case "ack": {
        const job = outgoing.get(msg.id);
        if (!job || job.pubkey !== pk) return;
        job.acked.set(msg.fid, Math.max(job.acked.get(msg.fid) || 0, msg.n));
        return;
      }
      case "ready":
        flags.add(`ready:${key(msg.id, msg.fid)}`);
        return;
      case "done":
        flags.add(`done:${key(msg.id, msg.fid)}`);
        return;
      case "file": {
        const acc = accepted.get(msg.id);
        if (!acc || acc.pubkey !== pk || !acc.fileIds.includes(msg.fid)) return;
        const current = receiving.get(pk);
        if (current && current.id === msg.id && current.fid === msg.fid) {   // a repeated header
          if (current.relay) reply(msg.id, pk, { k: "ready", id: msg.id, fid: msg.fid });
          return;
        }
        const relay = via === "relay";
        const rx = {
          id: msg.id, fid: msg.fid, name: baseName(msg.name), size: msg.size, relay,
          got: 0, written: 0, nextSeq: 0, pending: new Map(), ended: false, finished: false, error: null,
        };
        rx.queue = (async () => {
          try {
            const handle = await freeFileHandle(acc.dir, rx.name);
            rx.writable = await handle.createWritable();
          } catch (e) { rx.error = e; }
        })();
        receiving.set(pk, rx);
        if (relay) reply(msg.id, pk, { k: "ready", id: msg.id, fid: msg.fid });
        return;
      }
      case "end": {
        const rx = receiving.get(pk);
        if (!rx || rx.fid !== msg.fid) {
          if (finishedFiles.has(key(msg.id, msg.fid))) reply(msg.id, pk, { k: "done", id: msg.id, fid: msg.fid });
          return;
        }
        rx.ended = true;
        if (rx.written === rx.size) finishFile(pk, rx);   // otherwise the last write finishes it
        return;
      }
      case "rc":
        onRelayChunk(pk, msg);
        return;
      case "name": {
        const name = typeof msg.name === "string" ? msg.name.trim().slice(0, 40) : "";
        if (!name) return;
        names.set(pk, name);
        rememberMember(pk, name);
        emit({ event: "devices" });
        return;
      }
      case "text":
        if (!knownPeer(pk)) return;
        emit({ event: "message", from: nameOf(pk), kind: /^https?:\/\//i.test(msg.text) ? "url" : "text", text: msg.text });
        return;
      case "lan":
        if (via === "direct") onLan(pk, msg);
        return;
      case "cancel": {
        // The receiver stopped a file we were sending.
        const job = outgoing.get(msg.id);
        if (job && job.pubkey === pk && !cancelled.has(job.id)) {
          cancelled.add(job.id);
          outgoing.delete(job.id);
          emit({ event: "job_failed", job: job.id, error: "receiver stopped" });
        }
        // The sender cancelled: drop the offer or the file being written.
        incoming.delete(msg.id);
        accepted.delete(msg.id);
        const rx = receiving.get(pk);
        if (rx?.id === msg.id) { receiving.delete(pk); abortReceive(rx); }
        emit({ event: "offer_withdrawn", id: msg.id });
        return;
      }
    }
  }

  function abortReceive(rx) {
    rx.writable?.abort().catch(() => {});                // a partial file is discarded
  }

  function failReceive(pk, rx, e) {
    if (receiving.get(pk) === rx) receiving.delete(pk);
    abortReceive(rx);
    reply(rx.id, pk, { k: "cancel", id: rx.id });
    emit({ event: "error", message: `${rx.name}: ${e.message || e}` });
  }

  // Writes a chunk in arrival order and acknowledges it.
  function writeChunk(pk, rx, buf) {
    const every = rx.relay ? 4 * RELAY_CHUNK : 1;
    rx.queue = rx.queue.then(async () => {
      if (rx.error) throw rx.error;
      await rx.writable.write(buf);
      rx.written += buf.byteLength;
      if (rx.written % every === 0 || rx.written === rx.size) {
        reply(rx.id, pk, { k: "ack", id: rx.id, fid: rx.fid, n: rx.written });
      }
      if (rx.ended && rx.written === rx.size) finishFile(pk, rx);
    }).catch((e) => failReceive(pk, rx, e));
  }

  // Closes the file once every byte is on disk, then tells the sender it is complete.
  function finishFile(pk, rx) {
    if (rx.finished) return;
    rx.finished = true;
    rx.queue = rx.queue.then(async () => {
      if (rx.error) throw rx.error;
      await rx.writable.close();
      if (receiving.get(pk) === rx) receiving.delete(pk);
      finishedFiles.add(key(rx.id, rx.fid));
      emit({ event: "file_saved", from: nameOf(pk), file: rx.name, size: rx.size });
      reply(rx.id, pk, { k: "done", id: rx.id, fid: rx.fid });
    }).catch((e) => failReceive(pk, rx, e));
  }

  function onChunk(pk, buf) {
    const rx = receiving.get(pk);
    if (!rx || rx.relay) return;
    rx.got += buf.byteLength;
    if (rx.got > rx.size) {
      receiving.delete(pk);
      abortReceive(rx);
      emit({ event: "error", message: `${rx.name}: more data than announced` });
      return;
    }
    writeChunk(pk, rx, buf);
  }

  // Relay chunks can arrive out of order or twice: write them in sequence, once.
  function onRelayChunk(pk, msg) {
    const rx = receiving.get(pk);
    if (!rx || !rx.relay || rx.id !== msg.id || rx.fid !== msg.fid) return;
    if (msg.seq < rx.nextSeq || rx.pending.has(msg.seq)) return;
    rx.pending.set(msg.seq, fromB64(msg.d));
    while (rx.pending.has(rx.nextSeq)) {
      const buf = rx.pending.get(rx.nextSeq);
      rx.pending.delete(rx.nextSeq);
      rx.nextSeq++;
      rx.got += buf.byteLength;
      if (rx.got > rx.size) {
        failReceive(pk, rx, new Error("more data than announced"));
        return;
      }
      writeChunk(pk, rx, buf);
    }
  }

  // A device that left (on any link) drops its offers, jobs and files in progress.
  function dropPeer(pk) {
    for (const [id, o] of incoming) if (o.pubkey === pk) { incoming.delete(id); emit({ event: "offer_withdrawn", id }); }
    for (const job of outgoing.values()) {
      if (job.pubkey !== pk) continue;
      cancelled.add(job.id);
      outgoing.delete(job.id);
      emit({ event: "job_failed", job: job.id, error: "connection closed" });
    }
    const rx = receiving.get(pk);
    if (rx && !rx.relay) { receiving.delete(pk); abortReceive(rx); }
    for (const [id, a] of accepted) if (a.pubkey === pk) accepted.delete(id);
  }

  function attach(session, kind) {
    session.addEventListener("data", (e) => {
      const { peerPubkey: pk, data: payload } = e.detail;
      if (typeof payload !== "string") { onChunk(pk, payload); return; }
      let msg;
      try { msg = JSON.parse(payload); } catch { return; }
      onControl(pk, msg, "direct");
    });
    session.addEventListener("participants", () => emit({ event: "devices" }));
    session.addEventListener("error", (e) => emit({ event: "error", message: e.detail.message }));
    session.addEventListener("peer-open", (e) => {
      if (e.detail.unreliable) return;
      announceName(e.detail.peerPubkey);
      probeLan(e.detail.peerPubkey);
      emit({ event: "devices" });
    });
    session.addEventListener("peer-close", (e) => {
      const pk = e.detail.peerPubkey;
      if (e.detail.unreliable) return;
      const rtc = linkOf(pk);
      rtts.delete(pk);
      if (rtc) return;                                       // still reachable on the other link
      dropPeer(pk);
      emit({ event: "devices" });
    });
    // A device that reloads can leave a half-open peer behind on the other side.
    // wireweave keeps that entry, so its presence heartbeats never reconnect the
    // device. Drop any peer that has not opened within STALE_MS; the next heartbeat
    // then reconnects it. Uses wireweave's internal _closePeer, pinned with the submodule.
    const staleSince = new Map();
    setInterval(() => {
      const now = Date.now();
      for (const pk of Array.from(session.peers.keys())) {
        if (session.peers.get(pk).dc?.readyState === "open") { staleSince.delete(pk); continue; }
        const since = staleSince.get(pk) ?? now;
        staleSince.set(pk, since);
        if (now - since > STALE_MS && typeof session._closePeer === "function") {
          staleSince.delete(pk);
          session._closePeer(pk);
        }
      }
    }, 10000);
  }
  links.forEach(([s, kind]) => attach(s, kind));

  zone.addEventListener("connected", () => emit({ event: "connected" }));

  function startJob(pk, info, files, via) {
    const job = { id: newId(), pubkey: pk, info, files, state: "waiting", acked: new Map(), via };
    outgoing.set(job.id, job);
    routes.set(job.id, via);
    emit({ event: "job_started", job: job.id, info });
    return job;
  }

  // -- local network: a direct probe that never leaves the LAN ----------------

  function probeLan(pk) {
    const cur = lan.get(pk);
    if (cur && (cur.state === "pending" || Date.now() - cur.at < LAN_FRESH_MS)) return;
    if (!dcOf(pk) || ww.auth.pubkey > pk) return;        // the lower key starts the probe
    lan.set(pk, { state: "pending", at: Date.now() });
    startLanOffer(pk).catch(() => finishLan(pk, false));
  }

  function armLan(pk, pc) {
    const timer = setTimeout(() => finishLan(pk, false), LAN_TIMEOUT_MS);
    lanPcs.set(pk, { pc, timer });
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "connected") finishLan(pk, true);
      if (pc.connectionState === "failed") finishLan(pk, false);
    };
  }

  function finishLan(pk, ok) {
    const p = lanPcs.get(pk);
    if (!p) return;
    clearTimeout(p.timer);
    lanPcs.delete(pk);
    p.pc.close();
    lan.set(pk, { state: ok ? "yes" : "no", at: Date.now() });
    if (ok) announceName(pk);              // in case the first one was lost while the peer opened
    emit({ event: "devices" });
  }

  // No STUN and no TURN: only host candidates, so the probe connects only on the same LAN.
  async function startLanOffer(pk) {
    const pc = new RTCPeerConnection({ iceServers: [] });
    pc.createDataChannel("lan");
    armLan(pk, pc);
    await pc.setLocalDescription(await pc.createOffer());
    await gathered(pc);
    sendControl(pk, { k: "lan", t: "offer", sdp: pc.localDescription.sdp });
  }

  async function answerLan(pk, sdp) {
    lan.set(pk, { state: "pending", at: Date.now() });
    const pc = new RTCPeerConnection({ iceServers: [] });
    armLan(pk, pc);
    await pc.setRemoteDescription({ type: "offer", sdp });
    await pc.setLocalDescription(await pc.createAnswer());
    await gathered(pc);
    sendControl(pk, { k: "lan", t: "answer", sdp: pc.localDescription.sdp });
  }

  function onLan(pk, msg) {
    if (msg.t === "offer") answerLan(pk, msg.sdp).catch(() => finishLan(pk, false));
    else if (msg.t === "answer") {
      const p = lanPcs.get(pk);
      if (p) p.pc.setRemoteDescription({ type: "answer", sdp: msg.sdp }).catch(() => finishLan(pk, false));
    }
  }

  // -- distance and presence: sampled from the direct links --------------------

  async function sampleRtt(pk, pc) {
    try {
      const stats = await pc.getStats();
      for (const r of stats.values()) {
        if (r.type === "candidate-pair" && r.nominated && r.state === "succeeded" && r.currentRoundTripTime != null) {
          rtts.set(pk, Math.round(r.currentRoundTripTime * 1000));
          return;
        }
      }
    } catch { /* no stats yet */ }
  }

  function bucket(pk) {
    if (lan.get(pk)?.state === "yes") return "lan";
    const rtt = rtts.get(pk);
    if (rtt == null) return "-";
    return rtt < 20 ? "near" : rtt < 100 ? "mid" : "far";
  }

  function sample() {
    const now = Date.now();
    for (const [s, kind] of links) {
      for (const pk of s.peers.keys()) {
        const seen = seenVia.get(pk);
        const stale = !seen || now - seen.at > SEEN_MS;
        if (stale || seen.link !== "zone" || kind === "zone") seenVia.set(pk, { link: kind, at: now });
        const pc = s.peers.get(pk).pc;
        if (s.peers.get(pk).dc?.readyState === "open" && pc) sampleRtt(pk, pc);
      }
    }
    // Presence and distance changes re-render the radar; quiet samples do not.
    const pks = new Set([...seenVia.keys()].filter((pk) => statusOf(pk).reach !== "offline"));
    const signature = [...pks].sort().map((pk) => `${pk}:${statusOf(pk).reach}:${bucket(pk)}`).join("|");
    if (signature !== lastSignature) {
      lastSignature = signature;
      emit({ event: "devices" });
    }
  }

  function statusOf(pk) {
    const direct = Boolean(dcOf(pk));
    const seen = seenVia.get(pk);
    const fresh = Boolean(seen && Date.now() - seen.at < SEEN_MS);
    const reach = direct ? "direct" : fresh ? "relay" : "offline";
    return {
      reach,
      online: reach !== "offline",
      lan: lan.get(pk)?.state === "yes",
      rtt: direct ? rtts.get(pk) ?? null : null,
      link: seen?.link || null,
    };
  }

  // -- zone: paired devices share one room and stay in it -------------------

  async function joinZone(secret) {
    await zone.disconnect();
    await zone.connect(await zoneRoom(secret), { displayName: ANON });
  }

  // -- nearby: devices behind the same public address ------------------------

  async function refreshNetwork() {
    const key = await networkKey();
    if (key === networkKeyValue && networkState !== "checking") return;
    networkKeyValue = key;
    if (key) {
      networkState = "on";
      await nearby.disconnect();
      await nearby.connect(await nearbyRoom(key), { displayName: ANON });
    } else {
      networkState = "unavailable";
      await nearby.disconnect();
    }
    emit({ event: "network", state: networkState });
  }

  // -- relay messages: encrypted direct messages on the same relays ------------

  function onRelayMessage(peer, rumor, plaintext) {
    // Every message we send is also delivered back to us (the sender's own copy); skip it.
    if (!peer || peer === ww.auth.pubkey || rumor.pubkey === ww.auth.pubkey) return;
    if (Date.now() / 1000 - rumor.created_at > RELAY_FRESH_S) return;   // relays hand back old messages
    if (!knownPeer(peer)) return;
    let msg;
    try { msg = JSON.parse(plaintext); } catch { return; }
    if (msg.k === "rc") return onRelayChunk(peer, msg);
    onControl(peer, msg, "relay");
  }

  // -- pairing: a short-lived room per code, host approves the joiner -------

  function closePairing() {
    const p = pairing;
    if (!p) return;
    pairing = null;
    clearTimeout(p.ttl);
    clearTimeout(p.ack);
    p.session.disconnect().catch(() => {});
  }

  function onPairMessage(p, pk, raw) {
    if (typeof raw !== "string") return;
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.k === "hello") {
      if (p.peer) return;                       // one device per pairing code
      p.peer = pk;
      p.name = msg.name;
      emit(p.role === "host"
        ? { event: "pair_request", name: msg.name }
        : { event: "pair_waiting", name: msg.name });
      return;
    }
    if (pk !== p.peer) return;
    if (msg.k === "zone" && p.role === "joiner") {
      p.paired = true;                          // the close that follows is not a lost connection
      saveZone({ secret: msg.secret, host: msg.host, joined: Date.now() });
      rememberMember(pk, msg.host);
      p.session.send(pk, JSON.stringify({ k: "paired" }));
      emit({ event: "paired", name: msg.host });
      joinZone(msg.secret).catch((e) => emit({ event: "error", message: `zone: ${e.message}` }));
      setTimeout(() => { if (pairing === p) closePairing(); }, 1000);
    } else if (msg.k === "paired" && p.role === "host") {
      p.paired = true;
      emit({ event: "pair_done", name: p.name });
      closePairing();
    } else if (msg.k === "declined" && p.role === "joiner") {
      emit({ event: "pair_declined" });
      closePairing();
    }
  }

  async function openPairing(code, role) {
    closePairing();
    const session = createDataSession({ fsm: ww.fsm, xstate, relayPool: ww.pool, auth: ww.auth, namespace: NAMESPACE });
    const p = { session, role, peer: null, name: "", ttl: null, ack: null };
    pairing = p;
    p.ttl = setTimeout(() => { if (pairing === p) { closePairing(); emit({ event: "pair_expired" }); } }, PAIR_TTL_MS);
    session.addEventListener("peer-open", (e) => {
      if (e.detail.unreliable || pairing !== p) return;
      session.send(e.detail.peerPubkey, JSON.stringify({ k: "hello", name: alias, role }));
    });
    session.addEventListener("data", (e) => onPairMessage(p, e.detail.peerPubkey, e.detail.data));
    session.addEventListener("peer-close", (e) => {
      if (pairing === p && !p.paired && !e.detail.unreliable && e.detail.peerPubkey === p.peer) emit({ event: "pair_lost" });
    });
    try {
      await session.connect(await pairRoom(code), { displayName: ANON });
    } catch (e) {
      if (pairing === p) closePairing();
      emit({ event: "error", message: `pairing: ${e.message}` });
    }
  }

  async function answerPair(accept) {
    const p = pairing;
    if (!p || p.role !== "host" || !p.peer) return;
    if (!accept) {
      p.session.send(p.peer, JSON.stringify({ k: "declined" }));
      closePairing();
      emit({ event: "pair_declined" });
      return;
    }
    const z = loadZone() || { secret: newZoneSecret(), host: alias, joined: Date.now() };
    saveZone(z);
    rememberMember(p.peer, p.name);
    p.session.send(p.peer, JSON.stringify({ k: "zone", secret: z.secret, host: alias }));
    p.ack = setTimeout(() => { if (pairing === p) { emit({ event: "pair_done", name: p.name }); closePairing(); } }, PAIR_ACK_MS);
    await joinZone(z.secret);
  }

  // Every device the page knows about, with how it can be reached.
  function devicesWhere(filter) {
    const pks = new Set([...seenVia.keys()]);
    for (const [s] of links) for (const pk of s.peers.keys()) pks.add(pk);
    const out = [];
    for (const pk of pks) {
      const status = statusOf(pk);
      if (!status.online || !filter(pk, status)) continue;
      out.push({ pubkey: pk, name: nameOf(pk), ...status });
    }
    return out;
  }

  return {
    pubkey: () => ww.auth.pubkey,

    async join() {
      ww.auth.loadFromStorage() || ww.auth.generateKey();
      ww.pool.connect();
      dm.subscribe(({ peer, rumor, plaintext }) => onRelayMessage(peer, rumor, plaintext));
      const z = loadZone();
      await Promise.all([z ? joinZone(z.secret) : null, refreshNetwork()]);
      setInterval(() => refreshNetwork().catch(() => {}), NETWORK_CHECK_MS);
      setInterval(sample, SAMPLE_MS);
    },

    leave() {
      closePairing();
      dm.unsubscribe();
      return Promise.all([zone.disconnect(), nearby.disconnect()]);
    },

    zone: loadZone,

    async leaveZone() {
      closePairing();
      saveZone(null);
      await zone.disconnect();
      emit({ event: "devices" });
    },

    openPairing,
    closePairing,
    answerPair,

    network: () => networkState,

    // Local-network probe result for a device: "pending", "yes", "no", or undefined.
    lanState: (pk) => lan.get(pk)?.state,

    // Paired devices, online or not, as if they were in the room.
    members() {
      const out = new Map();
      for (const [pk, m] of Object.entries(loadZone()?.members || {})) {
        out.set(pk, { pubkey: pk, name: m.name, online: false, reach: "offline", lan: false, rtt: null, seen: m.seen });
      }
      for (const d of devicesWhere((pk) => isMember(pk))) {
        const remembered = out.get(d.pubkey);
        if (!remembered) continue;
        const name = d.name === d.pubkey.slice(0, 8) && remembered.name ? remembered.name : d.name;
        out.set(d.pubkey, { ...d, name, seen: Date.now() });
        rememberMember(d.pubkey, name);
      }
      return [...out.values()];
    },

    // Devices on the same local network that are not paired: visible, accept each transfer.
    nearby() {
      return devicesWhere((pk, status) => !isMember(pk) && status.reach === "direct" && status.lan);
    },

    // Changes the name other devices see: told to every connected device at once.
    setAlias(name) {
      alias = name;
      for (const d of devicesWhere((pk, s) => s.reach === "direct")) announceName(d.pubkey);
    },

    // via: "direct" (default) or "relay", when the device has no direct path and the user agreed.
    sendFiles(pk, entries, label, via = "direct") {
      const files = entries.map((e, i) => ({ fid: String(i), name: e.path, file: e.file }));
      const job = startJob(pk, { items: files.map((f) => f.name), to: label }, files, via);
      const offer = { k: "offer", id: job.id, from: alias, files: files.map((f) => ({ id: f.fid, name: f.name, size: f.file.size })) };
      sendVia(via, pk, offer).catch((e) => {
        outgoing.delete(job.id);
        emit({ event: "job_failed", job: job.id, error: e.message });
      });
      return job.id;
    },

    sendText(pk, text, label, via = "direct") {
      const job = startJob(pk, { items: [text.slice(0, 60)], to: label }, [], via);
      outgoing.delete(job.id);
      sendVia(via, pk, { k: "text", text }).then(
        () => emit({ event: "job_done", job: job.id, info: job.info }),
        (e) => emit({ event: "job_failed", job: job.id, error: e.message }),
      );
    },

    // dir: the folder the files are streamed into (a FileSystemDirectoryHandle).
    answer(id, accept, fileIds = [], dir = null) {
      const offer = incoming.get(id);
      if (!offer) return;
      incoming.delete(id);
      const yes = accept && fileIds.length && dir;
      if (yes) accepted.set(id, { pubkey: offer.pubkey, dir, fileIds });
      const msg = yes ? { k: "accept", id, files: fileIds } : { k: "decline", id };
      reply(id, offer.pubkey, msg).then(undefined, (e) => emit({ event: "error", message: `answer failed: ${e.message}` }));
    },

    cancel(id) {
      const job = outgoing.get(id);
      if (!job) return;
      cancelled.add(id);
      reply(id, job.pubkey, { k: "cancel", id });
      if (job.state === "waiting") {
        outgoing.delete(id);
        cancelled.delete(id);
        emit({ event: "job_cancelled", job: id });
      }
    },
  };
}
