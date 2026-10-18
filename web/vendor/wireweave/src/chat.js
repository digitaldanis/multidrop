const DELETION_LOOKBACK_S = 7 * 86400;
const DELETION_LIMIT = 300;

const hexChannelId = async (channelId, serverId) => {
  const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode((serverId || 'default') + ':' + channelId));
  return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, '0')).join('');
};

const countLeadingZeroBits = (hexId) => {
  let bits = 0;
  for (let i = 0; i < hexId.length; i++) {
    const nibble = parseInt(hexId[i], 16);
    if (nibble === 0) { bits += 4; continue; }
    bits += Math.clz32(nibble) - 28;
    break;
  }
  return bits;
};

const minePow = (getEventHash, template, difficulty, maxIterations = 2_000_000) => {
  const tags = (template.tags || []).filter((t) => t[0] !== 'nonce');
  for (let nonce = 0; nonce < maxIterations; nonce++) {
    const candidate = { ...template, tags: [...tags, ['nonce', String(nonce), String(difficulty)]] };
    const id = getEventHash(candidate);
    if (countLeadingZeroBits(id) >= difficulty) return candidate;
  }
  return template;
};

const PROFILE_MISS_TTL_MS = 10 * 60 * 1000;

export class Chat extends EventTarget {
  constructor({ relayPool, auth, getChannelContext = () => ({ channelId: null, serverId: '' }), isAdmin = () => false, isRoleOf = () => 'member', bans = null, mutes = null, getEventHash = null, powDifficulty = 0 }) {
    super();
    if (!relayPool || !auth) throw new Error('Chat: relayPool + auth required');
    this.pool = relayPool; this.auth = auth;
    this.getChannelContext = getChannelContext; this.isAdmin = isAdmin; this.isRoleOf = isRoleOf;

    this.bans = bans; this.mutes = mutes;

    this.getEventHash = getEventHash; this.powDifficulty = powDifficulty;
    this.activeChannelId = null;
    this.messages = [];
    this.profiles = new Map(); this.fetching = new Set();
    this._sendTimes = [];
    this.rateLimitMax = 5;
    this.rateLimitWindowMs = 10000;
  }

  _isBlocked(serverId, pubkey) {
    if (this.bans && (this.bans.isBanned(serverId, pubkey) || this.bans.isTimedOut(serverId, pubkey))) return true;
    if (this.mutes && this.mutes.isMuted(pubkey)) return true;
    return false;
  }

  rateLimitRetryAfterMs() {
    const now = Date.now();
    this._sendTimes = (this._sendTimes || []).filter(t => now - t < this.rateLimitWindowMs);
    if (this._sendTimes.length < this.rateLimitMax) return 0;
    return this._sendTimes[0] + this.rateLimitWindowMs - now;
  }

  async send(content, { announcement = false, replyTo = null } = {}) {
    const { channelId, serverId, channelType } = this.getChannelContext();
    if (!this.auth.isLoggedIn() || !channelId) return;
    if (this.bans && (this.bans.isBanned(serverId, this.auth.pubkey) || this.bans.isTimedOut(serverId, this.auth.pubkey))) {
      this._emit('send-blocked', { reason: 'banned-or-timed-out' });
      return;
    }

    const isAnnouncementPost = announcement || channelType === 'announcement';
    if (isAnnouncementPost && !this.isAdmin(serverId)) {
      this._emit('send-blocked', { reason: 'announcement-admin-only' });
      return;
    }
    const trimmed = content.trim(); if (!trimmed) return;
    const retryAfter = this.rateLimitRetryAfterMs();
    if (retryAfter > 0) {
      this._emit('rate-limited', { retryAfterMs: retryAfter });
      return;
    }
    this._sendTimes.push(Date.now());
    const chanHex = await hexChannelId(channelId, serverId);
    const tags = [['e', chanHex, '', 'root']];
    if (replyTo?.id) tags.push(['e', replyTo.id, '', 'reply']);
    if (isAnnouncementPost) tags.push(['t', 'announcement']);
    let template = { kind: 42, created_at: Math.floor(Date.now() / 1000), tags, content: trimmed, pubkey: this.auth.pubkey };
    if (this.powDifficulty > 0 && this.getEventHash) template = minePow(this.getEventHash, template, this.powDifficulty);
    const signed = await this.auth.sign(template);
    this.pool.publish(signed);
    this._addMessage(this._eventToMsg(signed));
  }

  async loadHistory(channelId) {
    const { serverId } = this.getChannelContext();
    if (this.activeChannelId) {
      this.pool.unsubscribe('chat-' + this.activeChannelId);
      this.pool.unsubscribe('chat-live-' + this.activeChannelId);
      this.pool.unsubscribe('chat-deletions-' + this.activeChannelId);
    }
    this.activeChannelId = channelId;
    this.messages = [];
    this.deletedIds = this.deletedIds || new Set();
    this._emit('messages', { list: [] });
    const chanHex = await hexChannelId(channelId, serverId);
    if (this.activeChannelId !== channelId) return;
    const collected = [];
    this.pool.subscribe('chat-' + channelId,
      [{ kinds: [42], '#e': [chanHex], limit: 50 }],
      (ev) => { if ((ev.tags || []).some(t => t[0] === 'e' && t[1] === chanHex) && !this._isBlocked(serverId, ev.pubkey) && !this.deletedIds.has(ev.id)) collected.push(this._eventToMsg(ev)); },
      () => {
        if (this.activeChannelId !== channelId) return;
        collected.sort((a, b) => a.timestamp - b.timestamp);
        this.messages = collected;
        this._emit('messages', { list: collected });
      });
    this.pool.subscribe('chat-live-' + channelId,
      [{ kinds: [42], '#e': [chanHex], since: Math.floor(Date.now() / 1000) }],
      (ev) => { if ((ev.tags || []).some(t => t[0] === 'e' && t[1] === chanHex) && !this._isBlocked(serverId, ev.pubkey) && !this.deletedIds.has(ev.id)) this._addMessage(this._eventToMsg(ev)); });

    this.pool.subscribe('chat-deletions-' + channelId,
      [{ kinds: [5], since: Math.floor(Date.now() / 1000) - DELETION_LOOKBACK_S, limit: DELETION_LIMIT }],
      (ev) => {
        const targetId = (ev.tags || []).find((t) => t[0] === 'e')?.[1];
        if (!targetId) return;
        const target = this.messages.find((m) => m.id === targetId);
        if (!target) { (this._pendingDeletes ||= new Map()).set(targetId, ev.pubkey); return; }
        if (target.userId !== ev.pubkey && !['owner', 'admin', 'moderator'].includes(this.isRoleOf(serverId, ev.pubkey))) return;
        this.deletedIds.add(targetId);
        if (target) { this.messages = this.messages.filter((m) => m.id !== targetId); this._emit('messages', { list: this.messages }); }
      });
  }

  async deleteMessage(id) {
    const msg = this.messages.find(m => m.id === id);
    if (!msg) return;
    const { serverId } = this.getChannelContext();
    const isAuthor = msg.userId === this.auth.pubkey;
    if (!isAuthor && !this.isAdmin(serverId)) throw new Error('Cannot delete: not author or admin');
    const signed = await this.auth.sign({ kind: 5, created_at: Math.floor(Date.now() / 1000), tags: [['e', id]], content: 'deleted' });
    this.pool.publish(signed);
    (this.deletedIds = this.deletedIds || new Set()).add(id);
    this.messages = this.messages.filter(m => m.id !== id);
    this._emit('messages', { list: this.messages });
  }

  _eventToMsg(event) {
    const tTags = (event.tags || []).filter(t => t[0] === 't').map(t => t[1]);
    this._fetchProfile(event.pubkey);
    const replyTag = (event.tags || []).find(t => t[0] === 'e' && t[3] === 'reply');
    let replyTo = null;
    if (replyTag) {
      const cached = this.messages.find(m => m.id === replyTag[1]);
      replyTo = cached ? { id: cached.id, userId: cached.userId, content: cached.content } : { id: replyTag[1] };
    }
    const imeta = (event.tags || []).find(t => t[0] === 'imeta');
    const field = (key) => { const f = imeta && imeta.slice(1).find(x => typeof x === 'string' && x.startsWith(key + ' ')); return f ? f.slice(key.length + 1) : null; };
    const media = imeta && field('url') ? { url: field('url'), mime: field('m') || '', size: Number(field('size')) || null } : null;
    return { id: event.id, type: 'text', userId: event.pubkey, content: event.content, timestamp: event.created_at * 1000, tags: tTags, replyTo, ...(media ? { media } : {}) };
  }

  _addMessage(msg) {
    if (this.messages.find(m => m.id === msg.id)) return;
    const pendingBy = this._pendingDeletes?.get(msg.id);
    if (pendingBy !== undefined) {
      this._pendingDeletes.delete(msg.id);
      if (pendingBy === msg.userId) { (this.deletedIds = this.deletedIds || new Set()).add(msg.id); return; }
    }
    let i = this.messages.length;
    while (i > 0 && this.messages[i - 1].timestamp > msg.timestamp) i--;
    this.messages = [...this.messages.slice(0, i), msg, ...this.messages.slice(i)];
    this._emit('message', { message: msg });
    this._emit('messages', { list: this.messages });
  }

  resolveProfile(pubkey) {
    const p = this.profiles.get(pubkey);
    if (p) return p.name || this.auth.npubShort(pubkey);
    this._fetchProfile(pubkey);
    return this.auth.npubShort(pubkey);
  }

  _fetchProfile(pubkey) {
    if (this.fetching.has(pubkey)) return;
    const missedAt = this._profileMiss?.get(pubkey);
    if (missedAt !== undefined && Date.now() - missedAt < PROFILE_MISS_TTL_MS) return;
    this.fetching.add(pubkey);
    this.pool.subscribe('profile-' + pubkey,
      [{ kinds: [0], authors: [pubkey] }],
      (event) => {
        const known = this._profileEvents?.get(pubkey);
        if (known && known >= event.created_at) return;
        (this._profileEvents ||= new Map()).set(pubkey, event.created_at);
        try { this.profiles.set(pubkey, JSON.parse(event.content)); this._emit('profile', { pubkey, profile: this.profiles.get(pubkey) }); } catch {}
      },
      () => {
        this.fetching.delete(pubkey);
        this.pool.unsubscribe('profile-' + pubkey);
        if (!this.profiles.has(pubkey)) (this._profileMiss ||= new Map()).set(pubkey, Date.now());
      });
  }

  updateProfile(pubkey, profile) { this.profiles.set(pubkey, profile); this._emit('profile', { pubkey, profile }); }

  _emit(t, d) { this.dispatchEvent(new CustomEvent(t, { detail: d })); }
}

export const createChat = (opts) => new Chat(opts);
