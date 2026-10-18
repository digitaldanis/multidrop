

const KIND_METADATA = 0;
const PROFILE_CACHE_TTL_MS = 300000;

export class Profile extends EventTarget {
  constructor({ relayPool, auth, fetchImpl = null }) {
    super();
    if (!relayPool || !auth) throw new Error('Profile: relayPool + auth required');
    this.pool = relayPool;
    this.auth = auth;
    this.fetch = fetchImpl || (typeof fetch !== 'undefined' ? fetch : null);
    this.cache = new Map();
    this.subs = new Map();
  }

  async publish(fields) {
    if (!this.auth.isLoggedIn()) throw new Error('Profile: not logged in');
    const existing = this.cache.get(this.auth.pubkey)?.profile || {};
    const next = { ...existing, ...fields };
    const signed = await this.auth.sign({
      kind: KIND_METADATA,
      created_at: Math.floor(Date.now() / 1000),
      tags: [],
      content: JSON.stringify(next)
    });
    this.pool.publish(signed);
    this.cache.set(this.auth.pubkey, { profile: next, fetchedAt: Date.now(), eventCreatedAt: signed.created_at });
    this._emit('updated', { pubkey: this.auth.pubkey, profile: next });
    return signed;
  }

  async fetchOnce(pubkey, { timeoutMs = 8000, forceRefresh = false } = {}) {
    const cached = this.cache.get(pubkey);
    if (cached && !forceRefresh && Date.now() - cached.fetchedAt < PROFILE_CACHE_TTL_MS) return cached.profile;
    return new Promise((resolve) => {
      const subId = 'profile-once-' + pubkey.slice(0, 16) + '-' + Math.random().toString(36).slice(2, 8);
      let settled = false;
      const timer = setTimeout(() => { if (!settled) { settled = true; this.pool.unsubscribe(subId); resolve(cached?.profile ?? null); } }, timeoutMs);
      let best = null;
      this.pool.subscribe(subId,
        [{ kinds: [KIND_METADATA], authors: [pubkey] }],
        (event) => {
          if (best && best.created_at >= event.created_at) return;
          try { best = { created_at: event.created_at, profile: JSON.parse(event.content) }; } catch {}
        },
        () => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          this.pool.unsubscribe(subId);
          if (best) {
            this.cache.set(pubkey, { profile: best.profile, fetchedAt: Date.now(), eventCreatedAt: best.created_at });
            this._emit('updated', { pubkey, profile: best.profile });
          }
          resolve(best?.profile ?? cached?.profile ?? null);
        });
    });
  }

  subscribe(pubkey, onUpdate) {
    if (this.subs.has(pubkey)) return this.subs.get(pubkey);
    const subId = 'profile-sub-' + pubkey.slice(0, 16);
    this.subs.set(pubkey, subId);
    this.pool.subscribe(subId,
      [{ kinds: [KIND_METADATA], authors: [pubkey] }],
      (event) => {
        if (event.pubkey !== pubkey || event.kind !== KIND_METADATA) return;
        const cached = this.cache.get(pubkey);
        if (cached && cached.eventCreatedAt >= event.created_at) return;
        let profile;
        try { profile = JSON.parse(event.content); } catch { return; }
        this.cache.set(pubkey, { profile, fetchedAt: Date.now(), eventCreatedAt: event.created_at });
        this._emit('updated', { pubkey, profile });
        onUpdate?.(profile);
      });
    return subId;
  }

  unsubscribe(pubkey) {
    const subId = this.subs.get(pubkey);
    if (subId) { this.pool.unsubscribe(subId); this.subs.delete(pubkey); }
  }

  getCached(pubkey) { return this.cache.get(pubkey)?.profile ?? null; }

  async verifyNip05(identifier, expectedPubkey) {
    if (!this.fetch) return false;
    const match = /^(?:([\w.+-]+)@)?([\w.-]+)$/.exec((identifier || '').trim());
    if (!match) return false;
    const localpart = match[1] || '_';
    const domain = match[2];
    if (!domain) return false;
    try {
      const res = await this.fetch('https://' + domain + '/.well-known/nostr.json?name=' + encodeURIComponent(localpart));
      if (!res.ok) return false;
      const body = await res.json();
      const resolved = body?.names?.[localpart];
      return typeof resolved === 'string' && resolved === expectedPubkey;
    } catch { return false; }
  }

  _emit(t, d) { this.dispatchEvent(new CustomEvent(t, { detail: d })); }
}

export const createProfile = (opts) => new Profile(opts);
