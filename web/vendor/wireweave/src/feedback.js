import { dtag } from './dtag.js';
import { createRoles } from './roles.js';

const TOPIC = 'wireweave-feedback';
const HEX_ID = /^[0-9a-f]{64}$/;
const STATUSES = new Set(['open', 'in_progress', 'resolved', 'closed']);
const FORBIDDEN_KEYS = new Set(['__proto__', 'prototype', 'constructor']);
let instanceCount = 0;
const MAX_CACHED_SCOPES = 8;

const plainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value) &&
  (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);

const textField = (value, field, maxLength, required = false) => {
  if (typeof value !== 'string') throw new TypeError('Feedback: ' + field + ' must be a string');
  const trimmed = value.trim();
  if (trimmed.length > maxLength || (required && !trimmed)) throw new Error('Feedback: invalid ' + field);
  if ((field === 'message' ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/).test(trimmed)) throw new Error('Feedback: invalid ' + field);
  return trimmed;
};

const metadataCopy = (value) => {
  if (!plainObject(value)) throw new TypeError('Feedback: metadata must be a JSON object');
  const seen = new WeakSet();
  let nodes = 0;
  const copy = (input, depth) => {
    if (++nodes > 2048 || depth > 8) throw new Error('Feedback: metadata exceeds structural limits');
    if (input === null || typeof input === 'string' || typeof input === 'boolean') return input;
    if (typeof input === 'number' && Number.isFinite(input)) return input;
    if (typeof input !== 'object' || (!Array.isArray(input) && !plainObject(input))) {
      throw new TypeError('Feedback: metadata must contain only JSON values');
    }
    if (seen.has(input)) throw new Error('Feedback: metadata cannot contain cycles');
    seen.add(input);
    const result = Array.isArray(input) ? [] : {};
    const keys = Object.keys(input);
    if (Array.isArray(input) && keys.length !== input.length) throw new Error('Feedback: metadata arrays must be dense');
    for (const key of keys) {
      if (FORBIDDEN_KEYS.has(key)) throw new Error('Feedback: unsafe metadata key');
      const descriptor = Object.getOwnPropertyDescriptor(input, key);
      if (!Object.hasOwn(descriptor, 'value')) throw new Error('Feedback: metadata cannot contain accessors');
      if (Array.isArray(input) && !/^(0|[1-9][0-9]*)$/.test(key)) throw new Error('Feedback: invalid metadata array');
      result[key] = copy(descriptor.value, depth + 1);
    }
    seen.delete(input);
    return result;
  };
  const result = copy(value, 0);
  if (new TextEncoder().encode(JSON.stringify(result)).length > 16384) throw new Error('Feedback: metadata exceeds 16384 bytes');
  return result;
};

const inputObject = (value, allowed) => {
  if (!plainObject(value)) throw new TypeError('Feedback: options must be an object');
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new Error('Feedback: unknown field ' + key);
    if (!Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), 'value')) throw new Error('Feedback: accessors are not allowed');
  }
  return value;
};

const messagePayload = (input, root) => {
  inputObject(input, root ? ['message', 'metadata', 'name', 'contact'] : ['message', 'metadata']);
  const result = { message: textField(input.message, 'message', 10000, true), metadata: metadataCopy(Object.hasOwn(input, 'metadata') ? input.metadata : {}) };
  if (root) {
    result.name = textField(Object.hasOwn(input, 'name') ? input.name : '', 'name', 120);
    result.contact = textField(Object.hasOwn(input, 'contact') ? input.contact : '', 'contact', 320);
  }
  return result;
};

const updatePayload = (input) => {
  inputObject(input, ['status', 'assignee', 'metadata']);
  const result = {};
  if (Object.hasOwn(input, 'status')) {
    if (!STATUSES.has(input.status)) throw new Error('Feedback: invalid status');
    result.status = input.status;
  }
  if (Object.hasOwn(input, 'assignee')) {
    if (typeof input.assignee !== 'string' || (input.assignee !== '' && !HEX_ID.test(input.assignee))) throw new Error('Feedback: assignee must be a public key or empty');
    result.assignee = input.assignee;
  }
  if (Object.hasOwn(input, 'metadata')) result.metadata = metadataCopy(input.metadata);
  if (!Object.keys(result).length) throw new Error('Feedback: update cannot be empty');
  return result;
};

const compareEvents = (a, b) => a.createdAt - b.createdAt || a.id.localeCompare(b.id);
const clone = (value) => JSON.parse(JSON.stringify(value));

export class Feedback extends EventTarget {
  constructor({ relayPool, auth = null, roles = null, serverId, timeoutMs = 8000, maxEvents = 2000 } = {}) {
    super();
    if (!relayPool || typeof relayPool.subscribe !== 'function' || typeof relayPool.unsubscribe !== 'function' ||
      typeof relayPool.publishAndWait !== 'function' || typeof relayPool.verifyEvent !== 'function') {
      throw new Error('Feedback: relayPool with event verification and publish acknowledgements required');
    }
    this.serverId = textField(serverId, 'serverId', 512, true);
    this.creator = this.serverId.split(':')[0].toLowerCase();
    if (!HEX_ID.test(this.creator)) throw new Error('Feedback: serverId must start with its creator public key');
    if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) throw new Error('Feedback: invalid timeoutMs');
    this.pool = relayPool;
    this.auth = auth;
    this.ownsRoles = !roles;
    this.roles = roles || createRoles({ relayPool, auth: auth || { pubkey: '' } });
    if (!Number.isInteger(maxEvents) || maxEvents < 1 || maxEvents > 10000) throw new Error('Feedback: maxEvents must be 1..10000');
    this.maxEvents = maxEvents;
    this.historyIncomplete = false;
    this.historyStatus = { queried: [], completed: [], completeAcrossRelays: false };
    this.timeoutMs = timeoutMs;
    this.roots = new Map();
    this.actions = new Map();
    this.seen = new Set();
    this.subscriptions = new Map();
    this.scopeStores = new Map();
    this.pendingHydrations = new Set();
    this.cacheGeneration = 0;
    this.selectedStore = { key: 'all', scope: {}, roots: this.roots, actions: this.actions, seen: this.seen, incomplete: false, live: 0, generation: 0, status: this.historyStatus };
    this.scopeStores.set('all', this.selectedStore);
    this.instanceId = ++instanceCount;
    this.sequence = 0;
    this.lastPublishedAt = 0;
    this.rolesListening = false;
    this.roleVersion = null;
    this.developers = new Set();
    this.roleSubscriptionId = 'feedback-roles-' + this.instanceId;
  }

  _isDeveloper(pubkey) {
    if (pubkey === this.creator) return true;
    return this.developers.has(pubkey);
  }

  _identity() {
    if (!HEX_ID.test(this.auth?.pubkey || '') || typeof this.auth?.sign !== 'function') {
      throw new Error('Feedback: signing identity required');
    }
    return this.auth.pubkey;
  }

  async submit(pageId, input) {
    const page = textField(pageId, 'pageId', 512, true);
    this._identity();
    return this._publish('submit', page, null, messagePayload(input, true));
  }

  async reply(threadId, input) {
    const thread = this._requiredThread(threadId);
    const author = this._identity();
    if (author !== thread.author && !this._isDeveloper(author)) throw new Error('Feedback: only the author or a developer may reply');
    return this._publish('reply', thread.pageId, thread.id, messagePayload(input, false));
  }

  async update(threadId, input) {
    const thread = this._requiredThread(threadId);
    if (!this._isDeveloper(this._identity())) throw new Error('Feedback: developer permission required');
    return this._publish('update', thread.pageId, thread.id, updatePayload(input));
  }

  _requiredThread(id) {
    if (typeof id !== 'string' || !HEX_ID.test(id)) throw new Error('Feedback: invalid threadId');
    const thread = this.get(id);
    if (!thread) throw new Error('Feedback: thread not loaded; call fetchOnce first');
    return thread;
  }

  async _publish(action, pageId, rootId, payload) {
    const timestamp = Math.max(Date.now(), this.lastPublishedAt + 1);
    this.lastPublishedAt = timestamp;
    const tags = [['t', TOPIC], ['s', this.serverId], ['l', pageId], ['a', action], ['v', '1'],
      ['nonce', crypto.randomUUID()], ['ms', String(timestamp)]];
    if (rootId) tags.push(['e', rootId, '', 'root']);
    const signed = await this.auth.sign({ kind: 1, created_at: Math.floor(timestamp / 1000), tags, content: JSON.stringify(payload) });
    const record = this._parse(signed);
    if (!record || record.author !== this.auth.pubkey || record.action !== action || record.pageId !== pageId ||
      record.rootId !== rootId || record.createdAt !== timestamp || JSON.stringify(record.payload) !== JSON.stringify(payload)) {
      throw new Error('Feedback: signer returned an invalid event');
    }
    if (!await this.pool.publishAndWait(signed, { timeoutMs: this.timeoutMs })) {
      const error = new Error('Feedback: relay did not confirm acceptance; delivery may still occur');
      error.code = 'FEEDBACK_DELIVERY_UNCONFIRMED';
      error.eventId = signed.id;
      throw error;
    }
    this._apply(signed);
    return signed;
  }

  _parse(event) {
    try {
      if (!event || event.kind !== 1 || !HEX_ID.test(event.id) || !HEX_ID.test(event.pubkey) ||
        !Number.isSafeInteger(event.created_at) || event.created_at < 0 || !Number.isSafeInteger(event.created_at * 1000) || !Array.isArray(event.tags) ||
        typeof event.content !== 'string' || event.content.length > 40000 || !this.pool.verifyEvent(event)) return null;
      const tag = (key) => {
        const values = event.tags.filter((item) => Array.isArray(item) && item[0] === key);
        if (values.length !== 1 || typeof values[0][1] !== 'string') throw new Error('Invalid routing tag');
        return values[0];
      };
      if (tag('t')[1] !== TOPIC || tag('s')[1] !== this.serverId || tag('v')[1] !== '1') return null;
      const pageId = textField(tag('l')[1], 'pageId', 512, true);
      if (pageId !== tag('l')[1]) return null;
      const action = tag('a')[1];
      if (!['submit', 'reply', 'update'].includes(action)) return null;
      const roots = event.tags.filter((item) => Array.isArray(item) && item[0] === 'e');
      let rootId = null;
      if (action === 'submit' && roots.length) return null;
      if (action !== 'submit') {
        if (roots.length !== 1 || !HEX_ID.test(roots[0][1]) || roots[0][3] !== 'root') return null;
        rootId = roots[0][1];
      }
      const millisecondTags = event.tags.filter((item) => Array.isArray(item) && item[0] === 'ms');
      let createdAt = event.created_at * 1000;
      if (millisecondTags.length) {
        const value = tag('ms')[1];
        if (!/^(0|[1-9][0-9]*)$/.test(value)) return null;
        createdAt = Number(value);
        if (!Number.isSafeInteger(createdAt) || Math.floor(createdAt / 1000) !== event.created_at) return null;
      }
      const data = JSON.parse(event.content);
      const payload = action === 'update' ? updatePayload(data) : messagePayload(data, action === 'submit');
      return { id: event.id, author: event.pubkey, pageId, rootId, action, payload, createdAt, createdSecond: event.created_at };
    } catch { return null; }
  }

  _scope(options) {
    if (options.threadId !== undefined) {
      if (typeof options.threadId !== 'string' || !HEX_ID.test(options.threadId)) throw new Error('Feedback: invalid threadId');
      if (options.pageId !== undefined) throw new Error('Feedback: choose pageId or threadId scope');
      return { threadId: options.threadId };
    }
    return options.pageId === undefined ? {} : { pageId: textField(options.pageId, 'pageId', 512, true) };
  }

  _scopeKey(scope) {
    return scope.threadId ? 'thread:' + scope.threadId : scope.pageId ? 'page:' + scope.pageId : 'all';
  }

  _newStore(scope) {
    return { key: this._scopeKey(scope), scope, roots: new Map(), actions: new Map(), seen: new Set(),
      incomplete: false, live: 0, generation: 0, status: { queried: [], completed: [], completeAcrossRelays: false, scope: { ...scope } } };
  }

  _selectStore(store) {
    this.selectedStore = store;
    this.roots = store.roots;
    this.actions = store.actions;
    this.seen = store.seen;
    this.historyIncomplete = store.incomplete;
    this.historyStatus = store.status;
  }

  _reserveScope(scope) {
    const key = this._scopeKey(scope);
    if (this.scopeStores.has(key) || this.scopeStores.size < MAX_CACHED_SCOPES) return;
    const idle = [...this.scopeStores.values()].find((store) => store.live === 0);
    if (!idle) throw new Error('Feedback: at most eight simultaneous live scopes are supported');
    this.scopeStores.delete(idle.key);
    if (idle === this.selectedStore) this._selectStore([...this.scopeStores.values()].at(-1));
  }

  _scopeStore(scope) {
    const key = this._scopeKey(scope);
    let store = this.scopeStores.get(key);
    if (!store) {
      this._reserveScope(scope);
      store = this._newStore(scope);
      this.scopeStores.set(key, store);
    }
    return store;
  }

  _matchesScope(record, scope) {
    if (scope.threadId) return record.id === scope.threadId || record.rootId === scope.threadId;
    return scope.pageId === undefined || record.pageId === scope.pageId;
  }

  _retain(store, record) {
    if (store.incomplete || store.seen.has(record.id) || !this._matchesScope(record, store.scope)) return false;
    if (store.seen.size >= this.maxEvents) {
      store.incomplete = true;
      if (store === this.selectedStore) this.historyIncomplete = true;
      this._emit('overflow', { maxEvents: this.maxEvents, scope: { ...store.scope } });
      return false;
    }
    store.seen.add(record.id);
    if (record.action === 'submit') store.roots.set(record.id, record);
    else {
      if (!store.actions.has(record.rootId)) store.actions.set(record.rootId, new Map());
      store.actions.get(record.rootId).set(record.id, record);
    }
    return true;
  }

  _apply(event, pageId) {
    const record = this._parse(event);
    if (!record || (pageId !== undefined && record.pageId !== pageId)) return;
    for (const store of this.scopeStores.values()) {
      if (!this._retain(store, record)) continue;
      const threadId = record.rootId || record.id;
      const thread = this._snapshot(store, threadId);
      if (thread) {
        this._emit('feedback', { threadId, thread, scope: { ...store.scope } });
        this._emit('list', { threads: this._listStore(store), scope: { ...store.scope } });
      }
    }
  }

  _requireCompleteHistory(store = this.selectedStore) {
    if (!store.incomplete) return;
    const error = new Error('Feedback: event capacity exceeded for this scope; hydrate a narrower page or thread scope or increase maxEvents');
    error.code = 'FEEDBACK_HISTORY_INCOMPLETE';
    error.scope = { ...store.scope };
    throw error;
  }

  _snapshot(store, threadId) {
    const root = store.roots.get(threadId);
    if (!root) return null;
    const thread = { id: root.id, serverId: this.serverId, pageId: root.pageId, author: root.author,
      ...clone(root.payload), createdAt: root.createdAt, updatedAt: root.createdAt,
      status: 'open', assignee: '', replies: [], updates: [] };
    const actions = [...(store.actions.get(threadId)?.values() || [])].sort(compareEvents);
    for (const action of actions) {
      if (action.pageId !== root.pageId || action.createdSecond < root.createdSecond) continue;
      const developer = this._isDeveloper(action.author);
      if (action.action === 'reply' && (developer || action.author === root.author)) {
        thread.replies.push({ id: action.id, author: action.author, ...clone(action.payload), createdAt: action.createdAt });
      } else if (action.action === 'update' && developer) {
        const payload = clone(action.payload);
        if (Object.hasOwn(payload, 'status')) thread.status = payload.status;
        if (Object.hasOwn(payload, 'assignee')) thread.assignee = payload.assignee;
        if (Object.hasOwn(payload, 'metadata')) thread.metadata = { ...thread.metadata, ...payload.metadata };
        thread.updates.push({ id: action.id, author: action.author, ...payload, createdAt: action.createdAt });
      } else continue;
      thread.updatedAt = Math.max(thread.updatedAt, action.createdAt);
    }
    return thread;
  }

  get(threadId) {
    const matching = [...this.scopeStores.values()]
      .filter((store) => store.roots.has(threadId) || store.scope.threadId === threadId)
      .sort((a, b) => b.generation - a.generation || Number(b.scope.threadId === threadId) - Number(a.scope.threadId === threadId));
    const complete = matching.find((store) => !store.incomplete);
    if (complete) return this._snapshot(complete, threadId);
    if (matching.length) this._requireCompleteHistory(matching[0]);
    this._requireCompleteHistory();
    return null;
  }

  _listStore(store, options = {}) {
    const { pageId, status, assignee, limit = 100 } = options;
    return [...store.roots.keys()].map((id) => this._snapshot(store, id))
      .filter((thread) => (pageId === undefined || thread.pageId === pageId) &&
        (status === undefined || thread.status === status) && (assignee === undefined || thread.assignee === assignee))
      .sort((a, b) => b.updatedAt - a.updatedAt || a.id.localeCompare(b.id)).slice(0, limit);
  }

  list(options = {}) {
    inputObject(options, ['pageId', 'status', 'assignee', 'limit']);
    const { pageId, status, assignee, limit = 100 } = options;
    if (pageId !== undefined) textField(pageId, 'pageId', 512, true);
    if (status !== undefined && !STATUSES.has(status)) throw new Error('Feedback: invalid status');
    if (assignee !== undefined && assignee !== '' && !HEX_ID.test(assignee)) throw new Error('Feedback: invalid assignee');
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw new Error('Feedback: limit must be 1..1000');
    let store = this.selectedStore;
    if (pageId !== undefined) {
      store = this.scopeStores.get(this._scopeKey({ pageId })) ||
        [...this.scopeStores.values()].reverse().find((candidate) => !candidate.scope.pageId && !candidate.scope.threadId) || store;
      if ((store.scope.pageId && store.scope.pageId !== pageId) || store.scope.threadId) {
        const error = new Error('Feedback: page scope not loaded; call fetchOnce with pageId');
        error.code = 'FEEDBACK_SCOPE_NOT_LOADED';
        throw error;
      }
    }
    this._requireCompleteHistory(store);
    return this._listStore(store, options);
  }

  _filter(options) {
    inputObject(options, ['pageId', 'threadId', 'timeoutMs', 'requireAllRelays', 'signal']);
    const filter = { kinds: [1], '#t': [TOPIC], '#s': [this.serverId] };
    if (options.pageId !== undefined) filter['#l'] = [textField(options.pageId, 'pageId', 512, true)];
    return filter;
  }

  _applyRoles(event) {
    try {
      if (event.kind !== 30078 || event.pubkey !== this.creator || !HEX_ID.test(event.id) ||
        !Number.isSafeInteger(event.created_at) || event.created_at < 0 || typeof event.content !== 'string' ||
        event.content.length > 160000 || !this.pool.verifyEvent(event)) return;
      const routing = event.tags.filter((tag) => tag[0] === 'd');
      if (routing.length !== 1 || routing[0][1] !== dtag('roles', this.serverId)) return;
      const version = { id: event.id, createdAt: event.created_at * 1000 };
      if (this.roleVersion && (version.createdAt < this.roleVersion.createdAt ||
        (version.createdAt === this.roleVersion.createdAt && version.id >= this.roleVersion.id))) return;
      const data = JSON.parse(event.content);
      if (!plainObject(data) || !Array.isArray(data.admins) || !Array.isArray(data.mods) ||
        data.admins.length + data.mods.length > 2048 ||
        ![...data.admins, ...data.mods].every((pubkey) => typeof pubkey === 'string' && HEX_ID.test(pubkey))) return;
      this.roleVersion = version;
      this.developers = new Set([...data.admins, ...data.mods]);
      for (const store of this.scopeStores.values()) {
        if (store.incomplete) continue;
        for (const id of store.roots.keys()) this._emit('feedback', { threadId: id, thread: this._snapshot(store, id), scope: { ...store.scope } });
        this._emit('list', { threads: this._listStore(store), scope: { ...store.scope } });
      }
    } catch {}
  }

  _roleFilter() {
    return { kinds: [30078], authors: [this.creator], '#d': [dtag('roles', this.serverId)] };
  }

  _listenRoles() {
    if (!this.rolesListening) {
      this.rolesListening = true;
      this.pool.subscribe(this.roleSubscriptionId, [this._roleFilter()], (event) => this._applyRoles(event));
    }
    this.roles?.subscribe?.(this.serverId);
  }

  subscribe(options = {}) {
    inputObject(options, ['pageId', 'threadId']);
    const scope = this._scope(options);
    const filter = this._filter(options);
    const store = this._scopeStore(scope);
    store.live++;
    this._selectStore(store);
    this._listenRoles();
    const id = 'feedback-' + this.instanceId + '-' + ++this.sequence;
    let stopped = false;
    const stop = () => {
      if (stopped) return;
      stopped = true;
      this.pool.unsubscribe(id);
      this.subscriptions.delete(id);
      const current = this.scopeStores.get(store.key);
      if (current) current.live = Math.max(0, current.live - 1);
      this._stopRoleListenerIfIdle();
    };
    this.subscriptions.set(id, stop);
    const filters = scope.threadId ? [{ ...filter, ids: [scope.threadId] }, { ...filter, '#e': [scope.threadId] }] : [filter];
    this.pool.subscribe(id, filters, (event) => {
      const record = this._parse(event);
      if (!record || !this._matchesScope(record, scope)) return;
      this._apply(event, scope.pageId);
    }, () => {
      const current = this.scopeStores.get(store.key);
      if (current && !current.incomplete) this._emit('list', { threads: this._listStore(current), scope: { ...scope } });
    });
    return stop;
  }

  fetchOnce(options = {}) {
    const filter = this._filter(options);
    const scope = this._scope(options);
    const timeoutMs = options.timeoutMs ?? this.timeoutMs;
    const requireAllRelays = options.requireAllRelays ?? false;
    const signal = options.signal;
    if (typeof requireAllRelays !== 'boolean') throw new Error('Feedback: requireAllRelays must be boolean');
    if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) throw new Error('Feedback: invalid timeoutMs');
    if (signal !== undefined && (!signal || typeof signal.addEventListener !== 'function' || typeof signal.removeEventListener !== 'function' || typeof signal.aborted !== 'boolean')) {
      throw new Error('Feedback: signal must be an AbortSignal');
    }
    const abortedError = () => {
      const error = new Error('Feedback: history fetch aborted');
      error.name = 'AbortError';
      error.code = 'FEEDBACK_FETCH_ABORTED';
      return error;
    };
    if (signal?.aborted) return Promise.reject(abortedError());
    if (this.pendingHydrations.size >= MAX_CACHED_SCOPES) throw new Error('Feedback: at most eight simultaneous history requests are supported');
    if (!this.scopeStores.has(this._scopeKey(scope)) && this.scopeStores.size >= MAX_CACHED_SCOPES &&
      [...this.scopeStores.values()].every((store) => store.live > 0)) {
      throw new Error('Feedback: at most eight simultaneous live scopes are supported');
    }
    const candidate = this._newStore(scope);
    this.pendingHydrations.add(candidate);
    this._listenRoles();
    const id = 'feedback-fetch-' + this.instanceId + '-' + ++this.sequence;
    const expectedUrls = new Set([...(this.pool.relays?.entries() || [])]
      .filter(([, relay]) => relay.ws?.readyState === 0 || relay.ws?.readyState === 1).map(([url]) => url));
    for (const url of this.pool.urls || []) expectedUrls.add(url);
    candidate.status = { queried: [...expectedUrls].sort(), completed: [], completeAcrossRelays: false, scope: { ...scope } };
    return new Promise((resolve, reject) => {
      let done = false;
      const receivedEose = new Set();
      const roleEose = new Set();
      const roleId = id + '-roles';
      const onAbort = () => finish(abortedError());
      const updateCompletion = () => {
        if (done) return;
        const completed = [...receivedEose].filter((url) => roleEose.has(url)).sort();
        const completeAcrossRelays = expectedUrls.size > 0 && completed.length >= expectedUrls.size;
        candidate.status = { queried: [...expectedUrls].sort(), completed, completeAcrossRelays, scope: { ...scope } };
        if (requireAllRelays ? completeAcrossRelays : completed.length > 0) finish();
      };
      const finish = (error) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        this.pool.unsubscribe(id);
        this.pool.unsubscribe(roleId);
        this.subscriptions.delete(id);
        this.pendingHydrations.delete(candidate);
        this._stopRoleListenerIfIdle();
        if (!error) {
          try { this._requireCompleteHistory(candidate); }
          catch (failure) { error = failure; }
        }
        if (error) {
          if (candidate.incomplete) {
            const previous = this.scopeStores.get(candidate.key);
            if (previous) { previous.incomplete = true; if (previous === this.selectedStore) this.historyIncomplete = true; }
          }
          this.historyStatus = { ...candidate.status, failed: true, eventCapacityExceeded: candidate.incomplete };
          reject(error);
          return;
        }
        try {
          this._reserveScope(scope);
          const previous = this.scopeStores.get(candidate.key);
          candidate.live = previous?.live || 0;
          candidate.generation = ++this.cacheGeneration;
          const records = [...candidate.roots.values(), ...[...candidate.actions.values()].flatMap((actions) => [...actions.values()])];
          for (const store of this.scopeStores.values()) {
            if (store.key === candidate.key || store.incomplete) continue;
            const changed = new Set();
            for (const record of records) {
              if (this._retain(store, record)) changed.add(record.rootId || record.id);
            }
            if (!store.incomplete && changed.size) {
              for (const threadId of changed) {
                const thread = this._snapshot(store, threadId);
                if (thread) this._emit('feedback', { threadId, thread, scope: { ...store.scope } });
              }
              this._emit('list', { threads: this._listStore(store), scope: { ...store.scope } });
            }
          }
          this.scopeStores.set(candidate.key, candidate);
          this._selectStore(candidate);
          resolve(this._listStore(candidate));
          for (const threadId of candidate.roots.keys()) this._emit('feedback', { threadId, thread: this._snapshot(candidate, threadId), scope: { ...scope } });
          this._emit('list', { threads: this._listStore(candidate), scope: { ...scope } });
        } catch (failure) { reject(failure); }
      };
      const timer = setTimeout(() => finish(new Error('Feedback: history fetch timed out; results may be incomplete')), timeoutMs);
      this.subscriptions.set(id, () => finish(new Error('Feedback: history fetch cancelled')));
      signal?.addEventListener('abort', onAbort, { once: true });
      if (signal?.aborted) { finish(abortedError()); return; }
      const filters = scope.threadId ? [{ ...filter, ids: [scope.threadId] }, { ...filter, '#e': [scope.threadId] }] : [filter];
      this.pool.subscribe(id, filters, (event) => {
        const record = this._parse(event);
        if (record) this._retain(candidate, record);
      }, (url) => {
        if (expectedUrls.has(url)) receivedEose.add(url);
        updateCompletion();
      });
      this.pool.subscribe(roleId, [this._roleFilter()], (event) => this._applyRoles(event), (url) => {
        if (expectedUrls.has(url)) roleEose.add(url);
        updateCompletion();
      });
    });
  }

  unsubscribe() {
    for (const stop of [...this.subscriptions.values()]) stop();
    this._stopRoleListenerIfIdle();
  }

  _stopRoleListenerIfIdle() {
    if (!this.subscriptions.size && this.rolesListening) {
      this.pool.unsubscribe(this.roleSubscriptionId);
      this.rolesListening = false;
      if (this.ownsRoles) this.roles.unsubscribe(this.serverId);
    }
  }

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}

export const createFeedback = (options) => new Feedback(options);
