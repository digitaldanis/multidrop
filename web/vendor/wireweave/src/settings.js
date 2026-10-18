import { dtag, replaceableTs } from './dtag.js';

const VALID_BITRATES = [8000, 16000, 24000, 48000, 96000];
const clampBitrate = (v) => VALID_BITRATES.reduce((prev, cur) => Math.abs(cur - v) < Math.abs(prev - v) ? cur : prev);

export class Settings extends EventTarget {
  constructor({ relayPool, auth, roles }) {
    super();
    if (!relayPool || !auth || !roles) throw new Error('Settings: deps required');
    this.pool = relayPool; this.auth = auth; this.roles = roles;
    this.store = new Map();
    this.subs = new Map();
  }

  getBitrate(serverId) { return this.store.get(serverId)?.opusBitrate || 24000; }

  async setBitrate(serverId, bitrate) {
    if (!this.roles.isOwner(serverId) && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    const clamped = clampBitrate(Number(bitrate));
    const existing = this.store.get(serverId) || {};
    const next = { ...existing, opusBitrate: clamped };
    this.store.set(serverId, next);
    const signed = await this.auth.sign({ kind: 30078, created_at: replaceableTs(), tags: [['d', dtag('settings', serverId)]], content: JSON.stringify(next) });
    this.pool.publish(signed);
    this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, next } }));
    return clamped;
  }

  getEmbedAllowlist(serverId) {
    const s = this.store.get(serverId);
    if (!s?.embedAllowlist) return [];
    return typeof s.embedAllowlist === 'string' ? s.embedAllowlist.split(',').map(d => d.trim()).filter(Boolean) : s.embedAllowlist;
  }

  async setEmbedAllowlist(serverId, domainsStr) {
    if (!this.roles.isOwner(serverId) && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    const domains = domainsStr.split(',').map(d => d.trim()).filter(Boolean);
    const existing = this.store.get(serverId) || {};
    const next = { ...existing, embedAllowlist: domains };
    this.store.set(serverId, next);
    const signed = await this.auth.sign({ kind: 30078, created_at: replaceableTs(), tags: [['d', dtag('settings', serverId)]], content: JSON.stringify(next) });
    this.pool.publish(signed);
    this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, next } }));
    return domains;
  }

  isOriginAllowed(serverId, origin) {
    if (!origin) return false;
    const allowlist = this.getEmbedAllowlist(serverId);
    if (!allowlist.length) return true;
    let url; try { url = new URL(origin); } catch { return false; }
    return allowlist.some(p => {
      if (p === '*') return true;
      if (p.startsWith('*.')) { const s = p.slice(2); return url.hostname === s || url.hostname.endsWith('.' + s); }
      return url.hostname === p || url.hostname === p.replace(/^https?:\/\//, '');
    });
  }

  subscribe(serverId) {
    if (this.subs.has(serverId)) return;
    if (!serverId) return;
    const creator = serverId.split(':')[0].toLowerCase();
    if (!creator) return;
    const subId = 'settings-' + serverId;
    this.subs.set(serverId, subId);
    let settingsTs = 0;
    this.pool.subscribe(subId,
      [{ kinds: [30078], authors: [creator], '#d': [dtag('settings', serverId)] }],
      (event) => {
        if (event.pubkey !== creator || event.created_at < settingsTs) return;
        settingsTs = event.created_at;
        try { this.store.set(serverId, JSON.parse(event.content)); this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, next: this.store.get(serverId) } })); } catch {}
      });
  }

  unsubscribe(serverId) {
    const subId = this.subs.get(serverId);
    if (subId) { this.pool.unsubscribe(subId); this.subs.delete(serverId); }
  }
}

export const createSettings = (opts) => new Settings(opts);
