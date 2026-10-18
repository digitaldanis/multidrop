import { replaceableTs } from './dtag.js';
import { safeSetItem } from './safe-storage.js';

export class Servers extends EventTarget {
  constructor({ relayPool, auth, storage, onSwitch = null }) {
    super();
    if (!relayPool) throw new Error('Servers: relayPool required');
    if (!auth) throw new Error('Servers: auth required');
    if (!storage) throw new Error('Servers: storage required (no localStorage in this env — pass a {getItem,setItem,removeItem} adapter)');
    this.pool = relayPool; this.auth = auth; this.storage = storage; this.onSwitch = onSwitch;
    this.servers = []; this.currentServerId = null;
  }

  load() {
    try { this.servers = JSON.parse(this.storage.getItem('zn_servers') || '[]'); } catch { this.servers = []; }
    if (this.auth.pubkey) {
      this.pool.subscribe('my-servers', [{ kinds: [34550], authors: [this.auth.pubkey] }], (ev) => { if (ev.pubkey === this.auth.pubkey) this._handleEvent(ev); });
    }
    try {
      const joined = JSON.parse(this.storage.getItem('zn_joined_servers') || '[]');
      joined.forEach(sid => { if (!this.servers.find(s => s.id === sid)) this.servers = [...this.servers, { id: sid, name: sid.slice(0, 8), iconColor: '#5865F2' }]; });
      const unresolved = joined.filter(sid => { const p = sid.split(':'); return p.length === 2 && p[0] !== this.auth.pubkey; });
      const byAuthor = {};
      unresolved.forEach(sid => { const [author, dTag] = sid.split(':'); (byAuthor[author] = byAuthor[author] || []).push(dTag); });
      Object.keys(byAuthor).forEach(author => {
        this.pool.subscribe('joined-server-' + author.slice(0, 8),
          [{ kinds: [34550], authors: [author], '#d': byAuthor[author] }],
          (ev) => { if (ev.pubkey === author) this._handleEvent(ev); });
      });
    } catch {}
    this._emit('updated', { servers: this.servers });
  }

  _handleEvent(event) {
    const nameTag = event.tags.find(t => t[0] === 'name');
    const colorTag = event.tags.find(t => t[0] === 'color');
    const dTag = event.tags.find(t => t[0] === 'd');
    if (!dTag) return;
    const serverId = event.pubkey + ':' + dTag[1];
    const name = nameTag ? nameTag[1] : serverId.slice(0, 8);
    const iconColor = colorTag && /^#[0-9a-fA-F]{6}$/.test(colorTag[1]) ? colorTag[1] : '#5865F2';
    const existing = this.servers.find(s => s.id === serverId);
    if (existing) { existing.name = name; existing.iconColor = iconColor; existing.ownerId = event.pubkey; this.servers = [...this.servers]; }
    else this.servers = [...this.servers, { id: serverId, name, iconColor, ownerId: event.pubkey }];
    this._persist();
    this._emit('updated', { servers: this.servers });
  }

  async rename(serverId, name, iconColor = '#5865F2') {
    if (!serverId?.startsWith(this.auth.pubkey + ':')) throw new Error('Only owner can rename');
    name = (name || '').trim();
    if (!name) throw new Error('server name cannot be empty');
    const dTag = serverId.split(':')[1];
    const signed = await this.auth.sign({ kind: 34550, created_at: replaceableTs(), tags: [['d', dTag], ['name', name], ['color', iconColor]], content: '' });
    this.pool.publish(signed);
    const s = this.servers.find(x => x.id === serverId);
    if (s) { s.name = name; s.iconColor = iconColor; this.servers = [...this.servers]; this._persist(); this._emit('updated', { servers: this.servers }); }
  }

  async create(name, iconColor = '#5865F2') {
    const dTag = Math.random().toString(36).slice(2, 10);
    const serverId = this.auth.pubkey + ':' + dTag;
    const signed = await this.auth.sign({ kind: 34550, created_at: replaceableTs(), tags: [['d', dTag], ['name', name], ['color', iconColor]], content: '' });
    this.pool.publish(signed);
    this.servers = [...this.servers, { id: serverId, name, iconColor, ownerId: this.auth.pubkey }];
    this._persist();
    await this.switchTo(serverId);
  }

  async join(serverId, { name = null, iconColor = '#5865F2', select = true } = {}) {
    try {
      const joined = JSON.parse(this.storage.getItem('zn_joined_servers') || '[]');
      if (!joined.includes(serverId)) { joined.push(serverId); safeSetItem(this.storage, this, 'zn_joined_servers', JSON.stringify(joined)); }
    } catch {}
    if (!this.servers.find(s => s.id === serverId)) { this.servers = [...this.servers, { id: serverId, name: name || serverId.slice(0, 8), iconColor }]; this._persist(); this._emit('updated', { servers: this.servers }); }
    if (select) await this.switchTo(serverId);
  }

  async delete(serverId) {
    this.servers = this.servers.filter(s => s.id !== serverId);
    this._persist();
    try {
      const joined = JSON.parse(this.storage.getItem('zn_joined_servers') || '[]');
      safeSetItem(this.storage, this, 'zn_joined_servers', JSON.stringify(joined.filter(id => id !== serverId)));
    } catch {}
    if (this.currentServerId === serverId) await this.switchTo(null);
    else this._emit('updated', { servers: this.servers });
  }

  async leave(serverId) { return this.delete(serverId); }

  async switchTo(serverId) {
    this.currentServerId = serverId;
    if (serverId) safeSetItem(this.storage, this, 'zn_lastServer', serverId); else this.storage.removeItem('zn_lastServer');
    this._emit('switched', { serverId });
    await this.onSwitch?.(serverId);
  }

  init() {
    this.load();
    const last = this.storage.getItem('zn_lastServer');
    if (last && this.servers.find(s => s.id === last)) this.switchTo(last);
    else if (this.servers.length) this.switchTo(this.servers[0].id);
  }

  getOrder() { try { return JSON.parse(this.storage.getItem('zn_serverOrder') || '[]'); } catch { return []; } }
  saveOrder(ids) { safeSetItem(this.storage, this, 'zn_serverOrder', JSON.stringify(ids)); this._emit('updated', { servers: this.servers }); }
  sorted() {
    const order = this.getOrder();
    if (!order.length) return this.servers;
    const idx = {}; order.forEach((id, i) => idx[id] = i);
    return this.servers.slice().sort((a, b) => (idx[a.id] ?? Infinity) - (idx[b.id] ?? Infinity));
  }

  _persist() { safeSetItem(this.storage, this, 'zn_servers', JSON.stringify(this.servers)); }
  _emit(t, d) { this.dispatchEvent(new CustomEvent(t, { detail: d })); }
}

export const createServers = (opts) => new Servers(opts);
