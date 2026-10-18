import { dtag, replaceableTs } from './dtag.js';

const sameSet = (a, b) => a.length === b.length && [...a].sort().every((v, i) => v === [...b].sort()[i]);

export class Roles extends EventTarget {
  constructor({ relayPool, auth }) {
    super();
    if (!relayPool || !auth) throw new Error('Roles: deps required');
    this.pool = relayPool; this.auth = auth;
    this.store = new Map();
    this.subs = new Map();
  }

  _creatorOf(serverId) { return serverId ? serverId.split(':')[0].toLowerCase() : null; }

  isOwner(serverId) { return !!this.auth.pubkey && this._creatorOf(serverId) === this.auth.pubkey; }
  isAdmin(serverId) { if (this.isOwner(serverId)) return true; const r = this.store.get(serverId); return !!(r?.admins || []).includes(this.auth.pubkey); }
  isMod(serverId) { if (this.isAdmin(serverId)) return true; const r = this.store.get(serverId); return !!(r?.mods || []).includes(this.auth.pubkey); }

  getRole(serverId, pubkey) {
    if (pubkey === this._creatorOf(serverId)) return 'owner';
    const r = this.store.get(serverId) || {};
    if ((r.admins || []).includes(pubkey)) return 'admin';
    if ((r.mods || []).includes(pubkey)) return 'moderator';
    return 'member';
  }

  async setRole(serverId, targetPubkey, role) {
    if (!this.isOwner(serverId) && role === 'admin') throw new Error('Only owner can assign admin');
    if (!this.isAdmin(serverId)) throw new Error('Insufficient permissions');
    if (targetPubkey === this._creatorOf(serverId)) throw new Error('Cannot change the owner\'s role');
    if (!this.isOwner(serverId) && this.getRole(serverId, targetPubkey) === 'admin') {
      throw new Error('Only the owner can change another admin\'s role');
    }
    const existing = this.store.get(serverId) || { admins: [], mods: [] };
    let admins = (existing.admins || []).filter(p => p !== targetPubkey);
    let mods = (existing.mods || []).filter(p => p !== targetPubkey);
    if (role === 'admin') admins = [...admins, targetPubkey];
    else if (role === 'moderator') mods = [...mods, targetPubkey];
    const next = { admins, mods };
    const signed = await this.auth.sign({ kind: 30078, created_at: replaceableTs(), tags: [['d', dtag('roles', serverId)]], content: JSON.stringify(next) });
    this.store.set(serverId, next);
    this.pool.publish(signed);
    this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, next } }));
  }

  subscribe(serverId) {
    if (this.subs.has(serverId)) return;
    if (!serverId) return;
    const creator = this._creatorOf(serverId);
    if (!creator) return;
    const subId = 'roles-' + serverId;
    this.subs.set(serverId, subId);
    let rolesTs = 0;
    this.pool.subscribe(subId,
      [{ kinds: [30078], '#d': [dtag('roles', serverId)] }],
      (event) => {
        if (event.created_at < rolesTs) return;
        let data;
        try { data = JSON.parse(event.content); } catch { return; }
        const current = this.store.get(serverId) || { admins: [], mods: [] };
        const admins = data.admins || [];
        const signedByOwner = event.pubkey === creator;
        const signedByAdmin = (current.admins || []).includes(event.pubkey);
        if (!signedByOwner && !(signedByAdmin && sameSet(admins, current.admins || []))) return;
        rolesTs = event.created_at;
        this.store.set(serverId, { admins, mods: data.mods || [] });
        this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, next: this.store.get(serverId) } }));
      });
  }

  unsubscribe(serverId) {
    const subId = this.subs.get(serverId);
    if (subId) { this.pool.unsubscribe(subId); this.subs.delete(serverId); }
  }
}

export const createRoles = (opts) => new Roles(opts);
