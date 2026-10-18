const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

const mb = (bytes) => (bytes / (1024 * 1024)).toFixed(1);

const BLOSSOM_SERVERS = [
  'https://blossom.nostr.build',
  'https://files.sovbit.host',
  'https://nostrcheck.me'
];

const hashFile = async (file) => {
  const buf = await file.arrayBuffer();
  const h = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, '0')).join('');
};

const hexChannelId = async (channelId, serverId) => {
  const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode((serverId || 'default') + ':' + channelId));
  return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, '0')).join('');
};

export class Media {
  constructor({ relayPool, auth, servers = BLOSSOM_SERVERS }) {
    if (!relayPool || !auth) throw new Error('Media: deps required');
    this.pool = relayPool; this.auth = auth; this.servers = servers;
  }

  async _genAuth(file) {
    if (!this.auth.pubkey) throw new Error('Not authenticated');
    const hash = await hashFile(file);
    const signed = await this.auth.sign({
      kind: 24242, created_at: Math.floor(Date.now() / 1000),
      tags: [['t', 'upload'], ['x', hash], ['expiration', String(Math.floor(Date.now() / 1000) + 600)]],
      content: 'Upload ' + file.name
    });
    const bytes = new TextEncoder().encode(JSON.stringify(signed));
    let bin = '';
    bytes.forEach((b) => { bin += String.fromCharCode(b); });
    return { header: 'Nostr ' + btoa(bin), hash };
  }

  async _uploadBlossom(file, serverUrl) {
    const { header } = await this._genAuth(file);
    const res = await fetch(serverUrl + '/upload', {
      method: 'PUT', body: file,
      headers: { Authorization: header, 'Content-Type': file.type || 'application/octet-stream' }
    });
    if (!res.ok) throw new Error('status ' + res.status);
    const data = await res.json();
    const url = data.url || (data.content && JSON.parse(data.content).url);
    if (!url) throw new Error('no url in response');
    return url;
  }

  async upload(file) {
    const errors = [];
    for (const srv of this.servers) {
      try { return { url: await this._uploadBlossom(file, srv), type: file.type, name: file.name, size: file.size }; }
      catch (e) { errors.push(srv + ': ' + e.message); }
    }
    throw new Error('upload failed: ' + errors.join('; '));
  }

  async download(url, { onProgress = null } = {}) {
    const res = await fetch(url);
    if (!res.ok) throw new Error('download failed: ' + res.status);
    const total = Number(res.headers.get('content-length')) || 0;
    if (!onProgress || !res.body?.getReader) {
      const buf = await res.arrayBuffer();
      return { bytes: new Uint8Array(buf), type: res.headers.get('content-type') || '', size: buf.byteLength };
    }
    const reader = res.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      onProgress({ received, total });
    }
    const out = new Uint8Array(received);
    let off = 0;
    for (const c of chunks) { out.set(c, off); off += c.length; }
    return { bytes: out, type: res.headers.get('content-type') || '', size: received };
  }

  async fetchBlob(hash) {
    const errors = [];
    for (const srv of this.servers) {
      try {
        const url = srv.replace(/\/$/, '') + '/' + hash;
        const res = await fetch(url);
        if (!res.ok) { errors.push(srv + ': ' + res.status); continue; }
        const buf = await res.arrayBuffer();
        return { url, bytes: new Uint8Array(buf), type: res.headers.get('content-type') || '' };
      } catch (e) { errors.push(srv + ': ' + e.message); }
    }
    throw new Error('fetchBlob failed: ' + errors.join('; '));
  }

  isMedia(url) {
    if (typeof url !== 'string') return null;
    if (/\.(png|jpe?g|gif|webp|svg|avif|heic|heif|tiff?|bmp)(\?|$)/i.test(url)) return 'image';
    if (/\.(mp4|webm|mov|ogg|ogv|mkv|m4v|avi)(\?|$)/i.test(url)) return 'video';
    return null;
  }

  extractUrls(text) { return text ? (text.match(/https?:\/\/[^\s<>"]+/g) || []) : []; }

  async sendMedia(file, { channelId, serverId }) {
    if (file.size > MAX_UPLOAD_BYTES) throw new Error('file is ' + mb(file.size) + ' MB — the limit is ' + mb(MAX_UPLOAD_BYTES) + ' MB');
    const result = await this.upload(file);
    const chanHex = await hexChannelId(channelId, serverId);
    const imetaTag = ['imeta', 'url ' + result.url, 'm ' + result.type];
    if (result.size) imetaTag.push('size ' + result.size);
    const signed = await this.auth.sign({
      kind: 42, created_at: Math.floor(Date.now() / 1000),
      tags: [['e', chanHex, '', 'root'], imetaTag], content: result.url
    });
    this.pool.publish(signed);
    return { signed, result };
  }
}

export const createMedia = (opts) => new Media(opts);
