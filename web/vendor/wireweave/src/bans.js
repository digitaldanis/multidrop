import { dtag, parseDtag, replaceableTs } from './dtag.js';

const AUDIT_LOG_MAX = 200;

export class Bans extends EventTarget {
  constructor({ relayPool, auth = null, roles = null }) {
    super();
    if (!relayPool) throw new Error('Bans: relayPool required');
    this.pool = relayPool; this.auth = auth; this.roles = roles;
    this.store = new Map();
    this.subs = new Map();

    this.auditLog = [];
  }

  isBanned(serverId, pubkey) { return !!(this.store.get(serverId)?.banned || []).includes(pubkey); }
  isKicked(serverId, pubkey) { return !!(this.store.get(serverId)?.kicked || []).includes(pubkey); }
  isMuted(serverId, channelId, pubkey) { return !!(this.store.get(serverId)?.muted?.[channelId] || []).includes(pubkey); }

  isTimedOut(serverId, pubkey) {
    const t = this.store.get(serverId)?.timeouts?.[pubkey];
    return !!t && t.expiry > Math.floor(Date.now() / 1000);
  }

  _assertCanTarget(serverId, targetPubkey) {
    if (!this.roles) return;
    if (this.roles.isOwner(serverId)) return;
    const targetRole = this.roles.getRole(serverId, targetPubkey);
    if (targetRole === 'owner') throw new Error('Cannot take action against the server owner');
    if (targetRole === 'admin') throw new Error('Only the owner can take action against another admin');
  }

  async ban(serverId, pubkey) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    this._assertCanTarget(serverId, pubkey);
    const dTag = dtag('ban', serverId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dTag], ['server', serverId]],
      content: JSON.stringify({ action: 'ban', pubkey, timestamp: Math.floor(Date.now() / 1000) })
    });
    this.pool.publish(signed);
  }

  async unban(serverId, pubkey) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    const dTag = dtag('unban', serverId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dTag], ['server', serverId]],
      content: JSON.stringify({ action: 'unban', pubkey, timestamp: Math.floor(Date.now() / 1000) })
    });
    this.pool.publish(signed);
  }

  async timeout(serverId, pubkey, minutes) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    this._assertCanTarget(serverId, pubkey);
    const expiry = Math.floor(Date.now() / 1000) + (minutes * 60);
    const dTag = dtag('timeout', serverId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dTag], ['server', serverId]],
      content: JSON.stringify({ action: 'timeout', pubkey, expiry })
    });
    this.pool.publish(signed);
  }

  async clearTimeout(serverId, pubkey) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    this._assertCanTarget(serverId, pubkey);
    const dTag = dtag('timeout', serverId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dTag], ['server', serverId]],
      content: JSON.stringify({ action: 'timeout', pubkey, expiry: Math.floor(Date.now() / 1000) - 1 })
    });
    this.pool.publish(signed);
  }

  _mayModerate(serverId, author, target) {
    const creator = serverId.split(':')[0].toLowerCase();
    if (author === creator) return target !== creator;
    if (!this.roles || this.roles.getRole(serverId, author) !== 'admin') return false;
    return ['member', 'moderator'].includes(this.roles.getRole(serverId, target));
  }

  async kickFromVoice(serverId, pubkey) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && serverId && !this.roles.isAdmin(serverId)) throw new Error('Insufficient permissions');
    if (serverId) this._assertCanTarget(serverId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dtag('kick', pubkey)], ...(serverId ? [['server', serverId]] : [])], content: ''
    });
    this.pool.publish(signed);
  }

  async mute(serverId, channelId, pubkey) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && !this.roles.isMod(serverId)) throw new Error('Insufficient permissions');
    this._assertCanTarget(serverId, pubkey);
    const dTag = dtag('mute', serverId, channelId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dTag], ['server', serverId], ['channel', channelId]],
      content: JSON.stringify({ action: 'mute', pubkey, channelId, timestamp: Math.floor(Date.now() / 1000) })
    });
    this.pool.publish(signed);
  }

  async unmute(serverId, channelId, pubkey) {
    if (!this.auth?.isLoggedIn()) throw new Error('Not logged in');
    if (this.roles && !this.roles.isMod(serverId)) throw new Error('Insufficient permissions');
    const dTag = dtag('mute', serverId, channelId, pubkey);
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dTag], ['server', serverId], ['channel', channelId]],
      content: JSON.stringify({ action: 'unmute', pubkey, channelId, timestamp: Math.floor(Date.now() / 1000) })
    });
    this.pool.publish(signed);
  }

  getAuditLog(serverId = null) {
    return serverId ? this.auditLog.filter((e) => e.serverId === serverId) : this.auditLog.slice();
  }

  _recordAudit(entry) {
    this.auditLog.unshift(entry);
    if (this.auditLog.length > AUDIT_LOG_MAX) this.auditLog.length = AUDIT_LOG_MAX;
    this._emit('audit', { entry });
  }

  subscribe(serverId) {
    if (this.subs.has(serverId)) return;
    if (!serverId) return;
    const creator = serverId.split(':')[0].toLowerCase();
    if (!creator) return;
    const subId = 'bans-' + serverId;
    this.subs.set(serverId, subId);
    this.pool.subscribe(subId,
      [{ kinds: [30078], '#server': [serverId] }],
      (event) => {
        try {
          const dTag = event.tags.find(t => t[0] === 'd');
          if (!dTag?.[1]) return;
          const parsed = parseDtag(dTag[1]);
          if (!parsed || !['ban', 'unban', 'timeout', 'kick', 'mute'].includes(parsed.ns)) return;
          const pubkey = parsed.parts[parsed.parts.length - 1];
          if (!this._mayModerate(serverId, event.pubkey, pubkey)) return;
          const data = this.store.get(serverId) || { banned: [], timeouts: {}, kicked: [], muted: {}, _banTs: {} };
          data.muted = data.muted || {};
          data._banTs = data._banTs || {};

          const eventTs = Number.isFinite(event.created_at) ? event.created_at : Math.floor(Date.now() / 1000);

          if (parsed.ns === 'ban' && pubkey) {
            if ((data._banTs[pubkey] || 0) <= eventTs) {
              data._banTs[pubkey] = eventTs;
              if (!data.banned.includes(pubkey)) data.banned.push(pubkey);
            }
          } else if (parsed.ns === 'unban' && pubkey) {
            if ((data._banTs[pubkey] || 0) <= eventTs) {
              data._banTs[pubkey] = eventTs;
              data.banned = data.banned.filter((p) => p !== pubkey);
            }
          } else if (parsed.ns === 'kick' && pubkey) {
            data.kicked = data.kicked || [];
            if (!data.kicked.includes(pubkey)) data.kicked.push(pubkey);
          } else if (parsed.ns === 'timeout' && pubkey) {
            const body = JSON.parse(event.content);
            if (body.expiry > Math.floor(Date.now() / 1000)) (data.timeouts = data.timeouts || {})[pubkey] = { expiry: body.expiry };
            else if (data.timeouts?.[pubkey]) delete data.timeouts[pubkey];
          } else if (parsed.ns === 'mute' && pubkey) {
            const body = JSON.parse(event.content);

            const channelId = body.channelId ?? parsed.parts[1];
            data.muted[channelId] = data.muted[channelId] || [];
            if (body.action === 'unmute') data.muted[channelId] = data.muted[channelId].filter((p) => p !== pubkey);
            else if (!data.muted[channelId].includes(pubkey)) data.muted[channelId].push(pubkey);
          }
          this.store.set(serverId, data);
          this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, data } }));

          let body = null;
          try { body = JSON.parse(event.content); } catch {}
          this._recordAudit({ serverId, ns: parsed.ns, pubkey, actor: event.pubkey, action: body?.action || parsed.ns, at: eventTs, eventId: event.id });
        } catch {}
      });
  }

  unsubscribe(serverId) {
    const subId = this.subs.get(serverId);
    if (subId) { this.pool.unsubscribe(subId); this.subs.delete(serverId); }
  }
}

export const createBans = (opts) => new Bans(opts);
