import { safeSetItem } from './safe-storage.js';

const STORE_KEY_PREFIX = 'ww_msgbus_';
const OUTBOX_KEY_PREFIX = 'ww_msgbus_outbox_';
const PERSIST_DEBOUNCE_MS = 500;

export class MessageBus extends EventTarget {
  constructor({ maxMessages = 50, storage = null, roomKey = 'default', sendFn = null, isOnline = () => true } = {}) {
    super();
    this.max = maxMessages;
    this.messages = [];
    this.handlers = {};
    this.storage = storage;
    this.roomKey = roomKey;
    this.sendFn = sendFn;
    this.isOnline = isOnline;
    this.outbox = [];
    this._persistTimer = null;
    this._loadPersisted();
    this._loadOutbox();
  }

  handle(m) { this.handlers[m.type]?.(m); }
  register(type, fn) { this.handlers[type] = fn; }

  add(text, { audioData = null, userId = null, username = null } = {}) {
    const msg = { id: Date.now().toString(36) + Math.random().toString(36).slice(2), text, time: Date.now(), userId, username, audioData, pending: false };
    if (this.sendFn) {
      const online = this.isOnline();
      if (!online) {
        msg.pending = true;
        this._queueOutbox(msg);
      } else {
        try {
          const result = this.sendFn(msg);
          if (result === false) { msg.pending = true; this._queueOutbox(msg); }
        } catch {
          msg.pending = true;
          this._queueOutbox(msg);
        }
      }
    }
    this.messages = [...this.messages, msg];
    if (this.messages.length > this.max) this.messages = this.messages.slice(-this.max);
    this._schedulePersist();
    this.dispatchEvent(new CustomEvent('message', { detail: msg }));
    this.dispatchEvent(new CustomEvent('messages', { detail: { list: this.messages } }));
    return msg;
  }

  flushOutbox() {
    if (!this.sendFn || this.outbox.length === 0) return { sent: 0, remaining: this.outbox.length };
    const stillPending = [];
    let sent = 0;
    for (const msg of this.outbox) {
      let ok = false;
      try { ok = this.sendFn(msg) !== false; } catch { ok = false; }
      if (ok) {
        sent++;
        const local = this.messages.find((m) => m.id === msg.id);
        if (local) local.pending = false;
      } else {
        stillPending.push(msg);
      }
    }
    this.outbox = stillPending;
    this._persistOutbox();
    if (sent > 0) this.dispatchEvent(new CustomEvent('messages', { detail: { list: this.messages } }));
    this.dispatchEvent(new CustomEvent('outbox-flushed', { detail: { sent, remaining: this.outbox.length } }));
    return { sent, remaining: this.outbox.length };
  }

  getOutbox() { return this.outbox.slice(); }

  _queueOutbox(msg) {
    this.outbox.push(msg);
    this._persistOutbox();
  }

  _schedulePersist() {
    if (!this.storage) return;
    if (this._persistTimer) return;
    this._persistTimer = setTimeout(() => { this._persistTimer = null; this._persistNow(); }, PERSIST_DEBOUNCE_MS);
  }

  _persistNow() {
    if (!this.storage) return;
    safeSetItem(this.storage, this, STORE_KEY_PREFIX + this.roomKey, JSON.stringify(this.messages));
  }

  _persistOutbox() {
    if (!this.storage) return;
    safeSetItem(this.storage, this, OUTBOX_KEY_PREFIX + this.roomKey, JSON.stringify(this.outbox));
  }

  _loadPersisted() {
    if (!this.storage) return;
    try {
      const raw = this.storage.getItem(STORE_KEY_PREFIX + this.roomKey);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) this.messages = parsed.slice(-this.max);
    } catch {                                                      }
  }

  _loadOutbox() {
    if (!this.storage) return;
    try {
      const raw = this.storage.getItem(OUTBOX_KEY_PREFIX + this.roomKey);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) this.outbox = parsed;
    } catch {                                                    }
  }

  clear() {
    this.messages = [];
    this.outbox = [];
    if (this.storage) {
      try { this.storage.removeItem(STORE_KEY_PREFIX + this.roomKey); } catch {}
      try { this.storage.removeItem(OUTBOX_KEY_PREFIX + this.roomKey); } catch {}
    }
    this.dispatchEvent(new CustomEvent('messages', { detail: { list: this.messages } }));
  }
}

export const createMessageBus = (opts) => new MessageBus(opts);
