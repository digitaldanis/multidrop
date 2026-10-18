import { replaceableTs } from './dtag.js';
import { fragment, Reassembler, MTU_DEFAULT } from './frame.js';

const DEFAULT_ICE_SERVERS = [
  { urls: 'stun:stun.l.google.com:19302' },
  { urls: 'stun:stun1.l.google.com:19302' },
  { urls: 'stun:stun.cloudflare.com:3478' },
  { urls: 'stun:global.stun.twilio.com:3478' },
  { urls: 'turn:global.relay.metered.ca:80', username: 'openrelayproject', credential: 'openrelayproject' },
  { urls: 'turn:global.relay.metered.ca:80?transport=tcp', username: 'openrelayproject', credential: 'openrelayproject' },
  { urls: 'turn:global.relay.metered.ca:443', username: 'openrelayproject', credential: 'openrelayproject' },
  { urls: 'turns:global.relay.metered.ca:443?transport=tcp', username: 'openrelayproject', credential: 'openrelayproject' },

  { urls: 'turn:openrelay.metered.ca:443?transport=tcp', username: 'openrelayproject', credential: 'openrelayproject' }
];
let ICE_SERVERS = DEFAULT_ICE_SERVERS;
let iceServersOverridden = false;
export const setIceServers = (list) => { if (Array.isArray(list) && list.length) { ICE_SERVERS = list; iceServersOverridden = true; } };
export const getIceServers = () => ICE_SERVERS.slice();

let defaultTurnWarned = false;
const isHostedDeployment = () => {
  try {
    const host = typeof location !== 'undefined' && location.hostname;
    if (!host) return false;
    if (host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host === '') return false;
    if (host.endsWith('.local')) return false;
    return true;
  } catch { return false; }
};
const warnDefaultTurnCredentialsOnce = () => {
  if (defaultTurnWarned || iceServersOverridden) return;
  if (!isHostedDeployment()) return;
  defaultTurnWarned = true;
  console.warn('[wireweave] Using the bundled default TURN servers (openrelayproject shared public credentials) ' +
    'on a non-localhost deployment. These are a free, rate-limited, no-SLA relay meant for local dev/testing — ' +
    'NAT-restricted peers on this deployment may fail to connect or get throttled. Call setIceServers() with your ' +
    'own TURN credentials before connecting.');
};

const PRESENCE_EXPIRY = 300000;
const HEARTBEAT = 30000;
const DISCONNECT_GRACE = 8000;
const DC_LABEL = 'wireweave-data';

const DC_LABEL_UNRELIABLE = 'wireweave-data-unreliable';
const UNRELIABLE_DC_OPTIONS = { ordered: false, maxRetransmits: 0 };

const deriveRoomId = async (namespace, room) => {
  const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode((namespace || 'default') + ':data:' + room));
  return 'wwdata' + Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
};

const defaultCreatePeerConnection = (config) => new RTCPeerConnection(config);

export class DataSession extends EventTarget {
  constructor({ fsm, xstate, relayPool, auth, namespace = '', dataChannelOptions = { ordered: true }, iceServers = null, createPeerConnection = defaultCreatePeerConnection, mtu = MTU_DEFAULT, fragmentStaleMs = 10000 }) {
    super();
    if (!fsm || !xstate || !relayPool || !auth) throw new Error('DataSession: missing deps');
    this.fsm = fsm; this.xstate = xstate; this.pool = relayPool; this.auth = auth;
    this.namespace = namespace;
    this.dcOptions = dataChannelOptions;
    this.iceServers = iceServers || ICE_SERVERS;
    this.createPeerConnection = createPeerConnection;
    this.actor = null;
    this.room = ''; this.roomId = '';
    this.peers = new Map(); this.participants = new Map();
    this.heartbeat = null; this.joinTs = 0;
    this.retrySchedule = {};
    this.displayName = '';

    this.mtu = mtu;
    this.fragmentStaleMs = fragmentStaleMs;
    this._nextMessageId = 0;
  }

  _initActor() {
    const machine = this.fsm.dataMachine;
    if (!machine) throw new Error('DataSession: fsm.dataMachine missing');
    this.actor = this.xstate.createActor(machine);
    this.actor.subscribe((snap) => this.dispatchEvent(new CustomEvent('state', { detail: { value: snap.value } })));
    this.actor.start();
  }

  async connect(room, { displayName = 'Guest' } = {}) {
    warnDefaultTurnCredentialsOnce();
    if (!this.actor) this._initActor();
    if (!this.actor.getSnapshot().can({ type: 'connect' })) await this.disconnect();
    this.actor.send({ type: 'connect' });
    this.room = room;
    this.displayName = displayName;
    this.joinTs = Math.floor(Date.now() / 1000);
    try {
      this.roomId = await deriveRoomId(this.namespace, room);
      this.participants.clear();
      this.participants.set('local', { identity: displayName, isLocal: true, connectionQuality: 'good' });
      this.actor.send({ type: 'connected' });
      this._subscribeSignals();
      this._subscribePresence();
      await this._publishPresence('join');
      this._startHeartbeat();
      this._emit('connected', { roomId: this.roomId, room });
    } catch (e) {
      this.actor.send({ type: 'fail' });
      this._emit('error', { message: 'connect failed: ' + e.message });
      throw e;
    }
  }

  async disconnect() {
    if (!this.actor || this.actor.getSnapshot().matches('idle')) return;
    if (!this.actor.getSnapshot().can({ type: 'disconnect' })) return;
    this.actor.send({ type: 'disconnect' });
    await this._publishPresence('leave');
    this._stopHeartbeat();
    for (const pk of Array.from(this.peers.keys())) this._closePeer(pk);
    this.peers.clear();
    for (const pk of Object.keys(this.retrySchedule)) this._cancelReconnect(pk);
    if (this.roomId) {
      this.pool.unsubscribe('data-presence-' + this.roomId);
      this.pool.unsubscribe('data-signals-' + this.roomId);
    }
    this.participants.clear();
    this.roomId = ''; this.room = '';
    this.actor.send({ type: 'done' });
    this._emit('disconnected', {});
  }

  send(peerPubkey, payload) {
    const peer = this.peers.get(peerPubkey);
    if (!peer?.dc || peer.dc.readyState !== 'open') return false;
    try { peer.dc.send(payload); return true; } catch { return false; }
  }

  broadcast(payload) {
    let n = 0;
    for (const [, peer] of this.peers) {
      if (peer.dc?.readyState === 'open') {
        try { peer.dc.send(payload); n++; } catch {}
      }
    }
    return n;
  }

  sendUnreliable(peerPubkey, payload) {
    const peer = this.peers.get(peerPubkey);
    if (!peer?.dcUnreliable || peer.dcUnreliable.readyState !== 'open') return false;
    let frames;
    try { frames = fragment(payload, { messageId: this._nextMessageIdFor(), mtu: this.mtu }); }
    catch (e) { this._emit('error', { message: 'sendUnreliable fragment failed: ' + e.message }); return false; }
    try { for (const f of frames) peer.dcUnreliable.send(f); return true; }
    catch { return false; }
  }

  broadcastUnreliable(payload) {
    let frames;
    try { frames = fragment(payload, { messageId: this._nextMessageIdFor(), mtu: this.mtu }); }
    catch (e) { this._emit('error', { message: 'broadcastUnreliable fragment failed: ' + e.message }); return 0; }
    let n = 0;
    for (const [, peer] of this.peers) {
      if (peer.dcUnreliable?.readyState === 'open') {
        try { for (const f of frames) peer.dcUnreliable.send(f); n++; } catch {}
      }
    }
    return n;
  }

  _nextMessageIdFor() {
    const id = this._nextMessageId;
    this._nextMessageId = (this._nextMessageId + 1) & 0xffff;
    return id;
  }

  getParticipants() { return Array.from(this.participants.values()); }
  getPeers() { return Array.from(this.peers.keys()); }

  async _publishPresence(action) {
    if (!this.auth.isLoggedIn() || !this.roomId) return;
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', 'wireweave-data:' + this.roomId], ['action', action], ['room', this.room], ['ns', this.namespace]],
      content: JSON.stringify({ action, name: this.displayName, room: this.room, ts: Date.now() })
    });
    this.pool.publish(signed);
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this.heartbeat = setInterval(() => { if (this.actor?.getSnapshot().matches('connected')) this._publishPresence('heartbeat'); }, HEARTBEAT);
  }
  _stopHeartbeat() { if (this.heartbeat) { clearInterval(this.heartbeat); this.heartbeat = null; } }

  _subscribePresence() {
    this.pool.subscribe('data-presence-' + this.roomId,
      [{ kinds: [30078], '#d': ['wireweave-data:' + this.roomId] }],
      (event) => this._onPresence(event));
  }

  _subscribeSignals() {
    this.pool.subscribe('data-signals-' + this.roomId,
      [{ kinds: [30078], '#p': [this.auth.pubkey], '#r': [this.roomId] }],
      (event) => this._handleSignal(event));
  }

  _onPresence(event) {
    if (event.pubkey === this.auth.pubkey) return;
    let data; try { data = JSON.parse(event.content); } catch { return; }
    if (Date.now() - (data.ts || 0) > PRESENCE_EXPIRY) return;
    const shortId = 'nostr-' + event.pubkey.slice(0, 12);
    if (data.action === 'leave') { this.participants.delete(shortId); this._closePeer(event.pubkey); }
    else if (!this.participants.has(shortId)) {
      this.participants.set(shortId, { identity: data.name || event.pubkey.slice(0, 8), isLocal: false, connectionQuality: 'connecting' });
      this._maybeConnect(event.pubkey);
    } else if (!this.peers.has(event.pubkey)) this._maybeConnect(event.pubkey);
    this._emit('participants', { list: this.getParticipants() });
  }

  _maybeConnect(peerPubkey) {
    if (!peerPubkey || peerPubkey === this.auth.pubkey || this.peers.has(peerPubkey)) return;
    this._cancelReconnect(peerPubkey);
    const fsmActor = this.xstate.createActor(this.fsm.peerMachine);
    fsmActor.subscribe((snap) => { const p = this.peers.get(peerPubkey); if (p) p.state = snap.value; });
    fsmActor.start();
    const peer = { pc: null, dc: null, dcUnreliable: null, reassembler: new Reassembler({ staleMs: this.fragmentStaleMs }), pendingCandidates: [], bufferedCandidates: [], iceTimer: null, sentFirstCandidate: false, disconnectTimer: null, failCount: 0, state: 'new', fsm: fsmActor, remoteDescSet: false };
    this.peers.set(peerPubkey, peer);
    const pc = this.createPeerConnection({ iceServers: this.iceServers, bundlePolicy: 'max-bundle', iceCandidatePoolSize: 4, iceTransportPolicy: 'all' });
    peer.pc = pc;
    const isOfferer = this.auth.pubkey > peerPubkey;
    this._wirePeer(peer, peerPubkey, fsmActor, isOfferer);
  }

  _wirePeer(peer, peerPubkey, fsmActor, isOfferer) {
    const pc = peer.pc;
    pc.onicecandidate = (ev) => {
      if (!ev.candidate) return;

      if (!peer.sentFirstCandidate) {
        peer.sentFirstCandidate = true;
        this._publishSignal(peerPubkey, 'ice', [ev.candidate.toJSON()]);
        return;
      }
      peer.pendingCandidates.push(ev.candidate.toJSON());
      if (peer.iceTimer) clearTimeout(peer.iceTimer);
      peer.iceTimer = setTimeout(() => { if (peer.pendingCandidates.length) { this._publishSignal(peerPubkey, 'ice', peer.pendingCandidates.splice(0)); peer.iceTimer = null; } }, 500);
    };
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState === 'complete') {
        if (peer.iceTimer) { clearTimeout(peer.iceTimer); peer.iceTimer = null; }
        if (peer.pendingCandidates.length) this._publishSignal(peerPubkey, 'ice', peer.pendingCandidates.splice(0));
      }
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected') {
        peer.failCount = 0; this._cancelReconnect(peerPubkey);
        if (fsmActor.getSnapshot().can({ type: 'recv_answer' })) fsmActor.send({ type: 'recv_answer' });
        if (peer.disconnectTimer) { clearTimeout(peer.disconnectTimer); peer.disconnectTimer = null; }
      }
      if (pc.connectionState === 'disconnected') {
        fsmActor.send({ type: 'disconnect' });
        peer.disconnectTimer = setTimeout(() => this._doIceRestart(peer, peerPubkey, fsmActor), DISCONNECT_GRACE);
      }
      if (pc.connectionState === 'failed') this._doIceRestart(peer, peerPubkey, fsmActor);
      if (pc.connectionState === 'closed') this._closePeer(peerPubkey);
    };
    if (isOfferer) {

      try {
        const dc = pc.createDataChannel(DC_LABEL, this.dcOptions);
        peer.dc = dc; this._wireDataChannel(dc, peer, peerPubkey, false);
      } catch {}
      try {
        const dcU = pc.createDataChannel(DC_LABEL_UNRELIABLE, UNRELIABLE_DC_OPTIONS);
        peer.dcUnreliable = dcU; this._wireDataChannel(dcU, peer, peerPubkey, true);
      } catch {}
    } else {
      pc.ondatachannel = (ev) => {
        if (ev.channel.label === DC_LABEL) { peer.dc = ev.channel; this._wireDataChannel(peer.dc, peer, peerPubkey, false); }
        else if (ev.channel.label === DC_LABEL_UNRELIABLE) { peer.dcUnreliable = ev.channel; this._wireDataChannel(peer.dcUnreliable, peer, peerPubkey, true); }
      };
    }
    if (isOfferer) {
      fsmActor.send({ type: 'offer' });
      pc.createOffer().then(o => pc.setLocalDescription(o).then(() => this._publishSignal(peerPubkey, 'offer', o))).catch(() => {});
    }
  }

  _wireDataChannel(dc, peer, peerPubkey, unreliable) {
    dc.binaryType = 'arraybuffer';
    dc.onopen = () => this._emit('peer-open', { peerPubkey, unreliable });
    dc.onclose = () => this._emit('peer-close', { peerPubkey, unreliable });
    dc.onerror = (e) => this._emit('peer-error', { peerPubkey, unreliable, error: e });
    if (!unreliable) {
      dc.onmessage = (e) => this._emit('data', { peerPubkey, data: e.data, unreliable: false });
      return;
    }

    dc.onmessage = (e) => {
      let payload;
      try { payload = e.data instanceof ArrayBuffer ? e.data : (e.data?.buffer ?? e.data); } catch { return; }
      let out;
      try { out = peer.reassembler.feed(payload); } catch { return; }
      if (out) this._emit('data', { peerPubkey, data: out.buffer.byteLength === out.byteLength ? out.buffer : out.slice().buffer, unreliable: true });
    };
  }

  _doIceRestart(peer, peerPubkey, fsmActor) {
    const pc = peer.pc;
    if (peer.disconnectTimer) { clearTimeout(peer.disconnectTimer); peer.disconnectTimer = null; }
    peer.failCount++;
    if (peer.failCount <= 1 && this.auth.pubkey > peerPubkey) {
      fsmActor.send({ type: 'restart' }); pc.restartIce();
      pc.createOffer({ iceRestart: true })
        .then(o => pc.setLocalDescription(o).then(() => this._publishSignal(peerPubkey, 'offer', o)))
        .catch(() => this._closePeer(peerPubkey));
    } else { this._closePeer(peerPubkey); this._scheduleReconnect(peerPubkey, peer.failCount); }
  }

  _handleSignal(event) {
    const from = event.pubkey; if (from === this.auth.pubkey) return;
    let data; try { data = JSON.parse(event.content); } catch { return; }
    if (!data?.type) return;
    if (!this.peers.has(from)) this._maybeConnect(from);
    const peer = this.peers.get(from); if (!peer) return;
    const pc = peer.pc; const fsmActor = peer.fsm;
    const addCands = (cands) => cands.forEach(c => pc.addIceCandidate(new RTCIceCandidate(c)).catch(() => {}));
    const drainBuf = () => { addCands(peer.bufferedCandidates); peer.bufferedCandidates = []; };
    const doAnswer = async () => {
      if (fsmActor.getSnapshot().can({ type: 'recv_offer' })) fsmActor.send({ type: 'recv_offer' });
      await pc.setRemoteDescription(new RTCSessionDescription(data.data));
      peer.remoteDescSet = true; drainBuf();
      const a = await pc.createAnswer(); await pc.setLocalDescription(a);
      fsmActor.send({ type: 'sent_answer' });
      this._publishSignal(from, 'answer', a);
    };
    if (data.type === 'offer') {
      const polite = this.auth.pubkey < from; const collision = pc.signalingState !== 'stable';
      if (collision && !polite) return;
      if (collision && polite) { pc.setLocalDescription({ type: 'rollback' }).then(doAnswer).catch(() => {}); return; }
      doAnswer().catch(() => {});
    } else if (data.type === 'answer' && pc.signalingState === 'have-local-offer') {
      pc.setRemoteDescription(new RTCSessionDescription(data.data)).then(() => { peer.remoteDescSet = true; drainBuf(); }).catch(() => {});
    } else if (data.type === 'ice') {
      const cands = Array.isArray(data.data) ? data.data : [data.data];
      if (peer.remoteDescSet) addCands(cands); else peer.bufferedCandidates.push(...cands);
    }
  }

  async _publishSignal(toPubkey, type, data) {
    if (!this.auth.pubkey || !this.roomId) return;
    const d = 'wireweave-data-rtc:' + this.roomId + ':' + this.auth.pubkey + ':' + toPubkey + ':' + type + ':' + (type === 'ice' ? Date.now() : 'sdp');
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', d], ['p', toPubkey], ['r', this.roomId]],
      content: JSON.stringify({ type, data })
    });
    this.pool.publish(signed);
  }

  _closePeer(peerPubkey) {
    const peer = this.peers.get(peerPubkey); if (!peer) return;
    if (peer.iceTimer) clearTimeout(peer.iceTimer);
    if (peer.disconnectTimer) clearTimeout(peer.disconnectTimer);
    try { peer.dc?.close(); } catch {}
    try { peer.dcUnreliable?.close(); } catch {}
    try { peer.pc?.close(); } catch {}
    this.peers.delete(peerPubkey);
    this._emit('peer-closed', { peerPubkey });
  }

  _cancelReconnect(pk) { const e = this.retrySchedule[pk]; if (e) { clearTimeout(e.timer); delete this.retrySchedule[pk]; } }

  _scheduleReconnect(pk, attempt) {
    const a = attempt || 0; if (a >= 6) return;
    this._cancelReconnect(pk);
    const timer = setTimeout(() => {
      delete this.retrySchedule[pk];
      if (!this.peers.has(pk) && this.roomId) this._maybeConnect(pk);
    }, Math.min(2 ** a * 2000, 30000));
    this.retrySchedule[pk] = { attempt: a, timer };
  }

  debug() {
    const peers = [];
    for (const [pk, peer] of this.peers) {
      peers.push({
        pubkey: pk.slice(0, 12),
        fsmState: peer.fsm?.getSnapshot().value,
        connState: peer.pc?.connectionState,
        dcState: peer.dc?.readyState || null,
        dcUnreliableState: peer.dcUnreliable?.readyState || null,
        pendingFragmentSets: peer.reassembler?.pendingCount() ?? 0,
        candidates: peer.pendingCandidates.length,
        buffered: peer.bufferedCandidates.length
      });
    }
    return { fsm: this.actor?.getSnapshot().value, room: this.room, roomId: this.roomId, peers, participants: this.getParticipants(), retrySchedule: Object.keys(this.retrySchedule) };
  }

  _emit(t, d) { this.dispatchEvent(new CustomEvent(t, { detail: d })); }
}

export const createDataSession = (opts) => new DataSession(opts);
