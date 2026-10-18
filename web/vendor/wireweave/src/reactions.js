

const REACTION_TARGET_CAP = 500;
const REACTION_RESUB_DEBOUNCE_MS = 250;

export class Reactions extends EventTarget {
  constructor({ relayPool, auth }) {
    super();
    if (!relayPool || !auth) throw new Error('Reactions: relayPool + auth required');
    this.pool = relayPool; this.auth = auth;
    this.byTarget = new Map();
  }

  async react(targetEventId, targetAuthorPubkey, content = '+') {
    if (!this.auth.isLoggedIn()) throw new Error('Not logged in');
    if (!targetEventId) throw new Error('targetEventId required');
    const tags = [['e', targetEventId], ['k', '42']];
    if (targetAuthorPubkey) tags.push(['p', targetAuthorPubkey]);
    const signed = await this.auth.sign({ kind: 7, created_at: Math.floor(Date.now() / 1000), tags, content });
    this.pool.publish(signed);

    this._applyReaction(signed, { local: true });
    return signed;
  }

  async unreact(targetEventId) {
    if (!this.auth.isLoggedIn()) throw new Error('Not logged in');
    const mine = this.byTarget.get(targetEventId)?.get(this.auth.pubkey);
    if (!mine) return;
    const signed = await this.auth.sign({ kind: 5, created_at: Math.floor(Date.now() / 1000), tags: [['e', mine.id]], content: 'deleted' });
    this.pool.publish(signed);
    this.byTarget.get(targetEventId)?.delete(this.auth.pubkey);
    this._emitFor(targetEventId);
  }

  getFor(targetEventId) {
    const m = this.byTarget.get(targetEventId);
    if (!m) return [];
    const counts = new Map();
    for (const { content } of m.values()) counts.set(content, (counts.get(content) || 0) + 1);
    return Array.from(counts.entries()).map(([content, count]) => ({
      content, count,
      mine: m.get(this.auth.pubkey)?.content === content
    }));
  }

  subscribeMany(targetEventIds) {
    this.ids = this.ids || new Set();
    let added = false;
    for (const id of targetEventIds) if (id && !this.ids.has(id)) { this.ids.add(id); added = true; }
    while (this.ids.size > REACTION_TARGET_CAP) this.ids.delete(this.ids.values().next().value);
    if (!added) return;
    clearTimeout(this._resubTimer);
    this._resubTimer = setTimeout(() => this._resubscribe(), REACTION_RESUB_DEBOUNCE_MS);
  }

  _resubscribe() {
    if (this._subId) this.pool.unsubscribe(this._subId);
    this._subId = null;
    const ids = Array.from(this.ids);
    if (!ids.length) return;
    this._subId = 'reactions-live';
    this.pool.subscribe(this._subId, [{ kinds: [7], '#e': ids }], (event) => this._applyReaction(event));
  }

  _applyReaction(event, { local = false } = {}) {
    const targetTag = (event.tags || []).find(t => t[0] === 'e');
    if (!targetTag?.[1]) return;
    const targetId = targetTag[1];
    if (!this.byTarget.has(targetId)) this.byTarget.set(targetId, new Map());
    const m = this.byTarget.get(targetId);
    const existing = m.get(event.pubkey);
    if (!local && existing && existing.created_at > event.created_at) return;
    m.set(event.pubkey, { content: event.content || '+', id: event.id, created_at: event.created_at });
    this._emitFor(targetId);
  }

  _emitFor(targetId) {
    this.dispatchEvent(new CustomEvent('updated', { detail: { targetId, reactions: this.getFor(targetId) } }));
  }
}

export const createReactions = (opts) => new Reactions(opts);
