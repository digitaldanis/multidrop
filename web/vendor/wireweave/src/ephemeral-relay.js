

export class EphemeralRelay {
  constructor({ WebSocketServer, verifyEvent = null, port = 0 } = {}) {
    if (!WebSocketServer) throw new Error('EphemeralRelay: WebSocketServer required (e.g. ws\'s WebSocketServer)');
    this.verifyEvent = verifyEvent;
    this.events = [];
    this.clients = new Map();
    this.wss = new WebSocketServer({ port });
    this.wss.on('connection', (ws) => this._onConnection(ws));
  }

  get port() { return this.wss.address()?.port; }
  get url() { return 'ws://127.0.0.1:' + this.port; }

  _onConnection(ws) {
    this.clients.set(ws, new Map());
    ws.on('message', (raw) => {
      let msg;
      try { msg = JSON.parse(raw.toString()); } catch { return; }
      if (!Array.isArray(msg)) return;
      const [type] = msg;
      if (type === 'EVENT') this._handleEvent(ws, msg[1]);
      else if (type === 'REQ') this._handleReq(ws, msg[1], msg.slice(2));
      else if (type === 'CLOSE') this._handleClose(ws, msg[1]);
    });
    ws.on('close', () => this.clients.delete(ws));
    ws.on('error', () => {});
  }

  _handleEvent(ws, event) {
    if (!event?.id) return;
    if (this.verifyEvent) {
      let ok = false;
      try { ok = this.verifyEvent(event); } catch { ok = false; }
      if (!ok) { this._send(ws, ['OK', event.id, false, 'invalid: signature verification failed']); return; }
    }
    if (!this.events.find((e) => e.id === event.id)) {
      this.events.push(event);

      for (const [clientWs, subs] of this.clients) {
        for (const [subId, filters] of subs) {
          if (this._matches(event, filters)) this._send(clientWs, ['EVENT', subId, event]);
        }
      }
    }
    this._send(ws, ['OK', event.id, true, '']);
  }

  _handleReq(ws, subId, filters) {
    if (!subId) return;
    this.clients.get(ws)?.set(subId, filters);
    const matched = this.events.filter((e) => this._matches(e, filters));
    for (const e of matched) this._send(ws, ['EVENT', subId, e]);
    this._send(ws, ['EOSE', subId]);
  }

  _handleClose(ws, subId) {
    this.clients.get(ws)?.delete(subId);
  }

  _matches(event, filters) {
    if (!Array.isArray(filters) || filters.length === 0) return true;
    return filters.some((f) => this._matchesOne(event, f));
  }

  _matchesOne(event, f) {
    if (f.ids && !f.ids.includes(event.id)) return false;
    if (f.authors && !f.authors.includes(event.pubkey)) return false;
    if (f.kinds && !f.kinds.includes(event.kind)) return false;
    if (f.since != null && event.created_at < f.since) return false;
    if (f.until != null && event.created_at > f.until) return false;
    for (const key of Object.keys(f)) {
      if (!key.startsWith('#')) continue;
      const tagName = key.slice(1);
      const wanted = f[key];
      const has = (event.tags || []).some((t) => t[0] === tagName && wanted.includes(t[1]));
      if (!has) return false;
    }
    return true;
  }

  _send(ws, msg) {
    if (ws.readyState === 1) { try { ws.send(JSON.stringify(msg)); } catch {} }
  }

  close() {
    return new Promise((resolve) => {
      for (const ws of this.clients.keys()) { try { ws.close(); } catch {} }
      this.clients.clear();
      this.wss.close(() => resolve());
    });
  }
}

export const createEphemeralRelay = (opts) => new EphemeralRelay(opts);
