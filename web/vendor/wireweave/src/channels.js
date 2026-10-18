import { dtag, replaceableTs } from './dtag.js';

const DEFAULT_CATEGORIES = [
  { id: 'general', name: 'TEXT CHANNELS', position: 0 },
  { id: 'voice', name: 'VOICE CHANNELS', position: 1 }
];

const DEFAULT_CHANNELS = [
  { id: 'general', name: 'general', type: 'text', categoryId: 'general', position: 0 },
  { id: 'announcements', name: 'announcements', type: 'announcement', categoryId: 'general', position: 1 },
  { id: 'general-voice', name: 'General', type: 'voice', categoryId: 'voice', position: 0 }
];

export class Channels extends EventTarget {
  constructor({ relayPool, auth }) {
    super();
    if (!relayPool || !auth) throw new Error('Channels: relayPool + auth required');
    this.pool = relayPool; this.auth = auth;
    this.serverId = ''; this.channels = []; this.categories = [];
    this.loaded = false;
    this._known = new Map();
  }

  isOwner() { return !!(this.auth.pubkey && this.serverId && this.auth.pubkey === this.serverId.split(':')[0].toLowerCase()); }

  load(serverId, onReady) {
    if (this.serverId) this.pool.unsubscribe('channels-' + this.serverId);
    this.serverId = serverId;
    const known = this._known.get(serverId);
    this.channels = known ? known.channels.slice() : [];
    this.categories = known ? known.categories.slice() : [];
    this.loaded = !!known;
    const ownerPubkey = serverId.split(':')[0].toLowerCase();
    const dTag = dtag('channels', serverId);
    let channelsTs = 0;
    this.pool.subscribe('channels-' + serverId,
      [{ kinds: [30078], authors: [ownerPubkey], '#d': [dTag] }],
      (event) => {
        if (event.pubkey !== ownerPubkey || event.created_at < channelsTs) return;
        channelsTs = event.created_at;
        const hasTag = event.tags?.some(t => t[0] === 'd' && t[1] === dTag);
        if (!hasTag) return;
        try {
          const data = JSON.parse(event.content);
          if (!data || typeof data !== 'object' || !Array.isArray(data.channels) || !Array.isArray(data.categories)) return;
          this.channels = data.channels;
          this.categories = data.categories;
          this.loaded = true;
          this._remember();
          this._emit('updated', { channels: this.channels, categories: this.categories });
        } catch {}
      },
      () => {
        if (!this.channels.length) this._setDefaults();
        onReady?.();
      });
  }

  _remember() {
    if (!this.serverId) return;
    this._known.set(this.serverId, { channels: this.channels.slice(), categories: this.categories.slice() });
  }

  _setDefaults() {
    this.channels = DEFAULT_CHANNELS.map(c => ({ ...c }));
    this.categories = DEFAULT_CATEGORIES.map(c => ({ ...c }));
    this._emit('updated', { channels: this.channels, categories: this.categories, provisional: !this.loaded });
  }

  async _publish() {
    if (!this.isOwner()) return;
    const signed = await this.auth.sign({
      kind: 30078, created_at: replaceableTs(),
      tags: [['d', dtag('channels', this.serverId)]],
      content: JSON.stringify({ channels: this.channels, categories: this.categories })
    });
    this._remember();
    this.pool.publish(signed);
  }

  async _publishOrRevert([channels, categories]) {
    try { await this._publish(); }
    catch (e) { this.channels = channels; this.categories = categories; this._emit('updated', { channels, categories }); throw e; }
  }

  async create(name, type = 'text', categoryId = 'general', extra = {}) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    name = (name || '').trim();
    if (!name) throw new Error('channel name cannot be empty');
    if (this.channels.some(c => c.name === name)) throw new Error('a channel with that name already exists');
    const created = { id: 'ch-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), name, type, categoryId, position: this.channels.length, ...(extra.parentChannelId ? { parentChannelId: extra.parentChannelId } : {}) };
    this.channels = [...this.channels, created];
    await this._publishOrRevert(prev);
    this._emit('updated', { channels: this.channels, categories: this.categories });
    return created;
  }

  async rename(id, name) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    name = (name || '').trim();
    if (!name) throw new Error('channel name cannot be empty');
    if (this.channels.some(c => c.id !== id && c.name === name)) throw new Error('a channel with that name already exists');
    this.channels = this.channels.map(c => c.id === id ? { ...c, name } : c);
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async update(id, patch) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    if (!patch || typeof patch !== 'object') return;
    this.channels = this.channels.map(c => c.id === id ? { ...c, ...patch } : c);
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async remove(id) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    if (this.channels.length <= 1) throw new Error('cannot remove the last channel');
    this.channels = this.channels.filter(c => c.id !== id);
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async createCategory(name) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    this.categories = [...this.categories, { id: 'cat-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), name, position: this.categories.length }];
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async renameCategory(id, name) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    this.categories = this.categories.map(c => c.id === id ? { ...c, name } : c);
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async deleteCategory(id) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    this.categories = this.categories.filter(c => c.id !== id);
    this.channels = this.channels.map(c => c.categoryId === id ? { ...c, categoryId: null } : c);
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async reorder(catId, ids) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    ids.forEach((chId, idx) => { this.channels = this.channels.map(c => c.id === chId ? { ...c, position: idx, categoryId: catId } : c); });
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  async reorderCategories(ids) {
    if (!this.isOwner()) throw new Error('owner only');
    const prev = [this.channels, this.categories];
    ids.forEach((catId, idx) => { this.categories = this.categories.map(c => c.id === catId ? { ...c, position: idx } : c); });
    await this._publishOrRevert(prev); this._emit('updated', { channels: this.channels, categories: this.categories });
  }

  _emit(t, d) { this.dispatchEvent(new CustomEvent(t, { detail: d })); }
}

export const createChannels = (opts) => new Channels(opts);
