const ALLOWED_TAGS = new Set(['a','abbr','b','blockquote','br','code','div','em','h1','h2','h3','h4','h5','h6','hr','i','img','li','ol','p','pre','span','strong','sub','sup','table','tbody','td','th','thead','tr','u','ul']);
const ALLOWED_ATTRS = { a: new Set(['href','title','rel','target']), img: new Set(['src','alt','title','width','height']), '*': new Set(['class','id']) };
const SAFE_URL = /^(https?:|mailto:|#|\/)/i;

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));

const sanitizeNoDom = (html) => {
  let out = '';
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { out += escapeHtml(html.slice(i)); break; }
    out += escapeHtml(html.slice(i, lt));
    const gt = html.indexOf('>', lt);
    if (gt < 0) { out += escapeHtml(html.slice(lt)); break; }
    const raw = html.slice(lt + 1, gt);
    const closing = raw.startsWith('/');
    const body = closing ? raw.slice(1) : raw;
    const m = body.match(/^([a-zA-Z][a-zA-Z0-9]*)/);
    i = gt + 1;
    if (!m) continue;
    const tag = m[1].toLowerCase();
    if (!ALLOWED_TAGS.has(tag)) continue;
    if (closing) { out += '</' + tag + '>'; continue; }
    const attrs = [];
    const attrRe = /([a-zA-Z_:][a-zA-Z0-9_:.-]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/g;
    let am;
    const allowed = ALLOWED_ATTRS[tag] || new Set();
    while ((am = attrRe.exec(body.slice(m[0].length))) !== null) {
      const name = am[1].toLowerCase();
      const value = am[3] ?? am[4] ?? am[5] ?? '';
      if (/^on/.test(name)) continue;
      if (!allowed.has(name) && !ALLOWED_ATTRS['*'].has(name)) continue;
      if ((name === 'href' || name === 'src') && !SAFE_URL.test(value.trim())) continue;
      attrs.push(name + '="' + escapeHtml(value) + '"');
    }
    out += '<' + tag + (attrs.length ? ' ' + attrs.join(' ') : '') + '>';
  }
  return out;
};

const sanitize = (html) => {
  if (typeof document === 'undefined') return sanitizeNoDom(html || '');
  const el = document.createElement('div');
  el.innerHTML = html || '';
  const walk = (node) => {
    [...node.childNodes].forEach(child => {
      if (child.nodeType === 1) {
        const tag = child.tagName.toLowerCase();
        if (!ALLOWED_TAGS.has(tag)) { child.replaceWith(...child.childNodes); return; }
        const allowed = ALLOWED_ATTRS[tag] || new Set();
        [...child.attributes].forEach(a => {
          const name = a.name.toLowerCase();
          if (/^on/.test(name) || (!allowed.has(name) && !ALLOWED_ATTRS['*'].has(name))) child.removeAttribute(a.name);
          else if ((name === 'href' || name === 'src') && !SAFE_URL.test(a.value.trim())) child.removeAttribute(a.name);
        });
        walk(child);
      } else if (child.nodeType !== 3) child.remove();
    });
  };
  walk(el);
  return el.innerHTML;
};

import { dtag, replaceableTs } from './dtag.js';

export class Pages extends EventTarget {
  constructor({ relayPool, auth, roles }) {
    super();
    if (!relayPool || !auth || !roles) throw new Error('Pages: deps required');
    this.pool = relayPool; this.auth = auth; this.roles = roles;
    this.store = new Map(); this.subs = new Map();
  }

  _key(serverId, slug) { return dtag('page', serverId, slug); }
  getPages(serverId) { return Array.from((this.store.get(serverId) || new Map()).values()); }

  subscribe(serverId) {
    if (this.subs.has(serverId)) return;
    const creator = serverId?.split(':')[0];
    if (!creator) return;
    const subId = 'pages-' + serverId;
    this.subs.set(serverId, subId);
    const pageTs = new Map();
    this.pool.subscribe(subId,
      [{ kinds: [30078], authors: [creator] }],
      (event) => {
        if (event.pubkey !== creator) return;
        const dTag = (event.tags.find(t => t[0] === 'd') || [])[1] || '';
        const prefix = dtag('page', serverId) + ':';
        if (!dTag.startsWith(prefix)) return;
        if ((pageTs.get(dTag) ?? 0) > event.created_at) return;
        pageTs.set(dTag, event.created_at);
        const slug = dTag.slice(prefix.length); if (!slug) return;
        try {
          const data = JSON.parse(event.content);
          const pages = this.store.get(serverId) || new Map();
          if (data.deleted) pages.delete(slug);
          else pages.set(slug, { slug, title: data.title || slug, html: sanitize(data.html || ''), updatedAt: event.created_at, author: event.pubkey });
          this.store.set(serverId, pages);
          this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, pages: this.getPages(serverId) } }));
        } catch {}
      });
  }

  unsubscribe(serverId) {
    const subId = this.subs.get(serverId);
    if (subId) { this.pool.unsubscribe(subId); this.subs.delete(serverId); }
  }

  async publish(serverId, slug, title, html) {
    if (!this.roles.isAdmin(serverId)) throw new Error('Admin only');
    const safe = sanitize(html);
    const signed = await this.auth.sign({ kind: 30078, created_at: replaceableTs(), tags: [['d', this._key(serverId, slug)]], content: JSON.stringify({ title, html: safe }) });
    this.pool.publish(signed);
    const pages = this.store.get(serverId) || new Map();
    pages.set(slug, { slug, title, html: safe, updatedAt: Math.floor(Date.now() / 1000), author: this.auth.pubkey });
    this.store.set(serverId, pages);
    this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, pages: this.getPages(serverId) } }));
  }

  async deletePage(serverId, slug) {
    if (!this.roles.isAdmin(serverId)) throw new Error('Admin only');
    const signed = await this.auth.sign({ kind: 30078, created_at: replaceableTs(), tags: [['d', this._key(serverId, slug)]], content: JSON.stringify({ deleted: true }) });
    this.pool.publish(signed);
    const pages = this.store.get(serverId) || new Map();
    pages.delete(slug);
    this.store.set(serverId, pages);
    this.dispatchEvent(new CustomEvent('updated', { detail: { serverId, pages: this.getPages(serverId) } }));
  }
}

export const createPages = (opts) => new Pages(opts);
