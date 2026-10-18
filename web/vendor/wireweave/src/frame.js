

const MAGIC = 0xf7;
const HEADER_BYTES = 11;

export const MTU_DEFAULT = 16000;

export const MAX_FRAGMENTS = 0xffff;

export const maxPayloadBytes = (mtu = MTU_DEFAULT) => {
  const perFragment = mtu - HEADER_BYTES;
  return perFragment * MAX_FRAGMENTS;
};

const toUint8 = (payload) => {
  if (payload instanceof Uint8Array) return payload;
  if (payload instanceof ArrayBuffer) return new Uint8Array(payload);
  if (ArrayBuffer.isView(payload)) return new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength);
  throw new Error('frame: payload must be an ArrayBuffer or a TypedArray');
};

export const encodeHeader = ({ messageId, fragmentIndex, fragmentCount, totalPayloadLength }) => {
  const buf = new ArrayBuffer(HEADER_BYTES);
  const dv = new DataView(buf);
  dv.setUint8(0, MAGIC);
  dv.setUint16(1, messageId, true);
  dv.setUint16(3, fragmentIndex, true);
  dv.setUint16(5, fragmentCount, true);
  dv.setUint32(7, totalPayloadLength, true);
  return buf;
};

export const decodeHeader = (bytes) => {
  const u8 = toUint8(bytes);
  if (u8.byteLength < HEADER_BYTES) return null;
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  if (dv.getUint8(0) !== MAGIC) return null;
  return {
    messageId: dv.getUint16(1, true),
    fragmentIndex: dv.getUint16(3, true),
    fragmentCount: dv.getUint16(5, true),
    totalPayloadLength: dv.getUint32(7, true)
  };
};

export const fragment = (payload, { messageId, mtu = MTU_DEFAULT }) => {
  if (mtu <= HEADER_BYTES) throw new Error('frame: mtu must be greater than the ' + HEADER_BYTES + '-byte header');
  const u8 = toUint8(payload);
  const totalPayloadLength = u8.byteLength;
  const perFragment = mtu - HEADER_BYTES;
  const fragmentCount = Math.max(1, Math.ceil(totalPayloadLength / perFragment));
  if (fragmentCount > MAX_FRAGMENTS) {
    throw new Error('frame: payload of ' + totalPayloadLength + ' bytes needs ' + fragmentCount +
      ' fragments at mtu=' + mtu + ', exceeding the ' + MAX_FRAGMENTS + '-fragment wire-format ceiling ' +
      '(max ' + maxPayloadBytes(mtu) + ' bytes at this mtu)');
  }
  const out = new Array(fragmentCount);
  for (let i = 0; i < fragmentCount; i++) {
    const start = i * perFragment;
    const end = Math.min(start + perFragment, totalPayloadLength);
    const slice = u8.subarray(start, end);
    const frameBuf = new ArrayBuffer(HEADER_BYTES + slice.byteLength);
    new Uint8Array(frameBuf, 0, HEADER_BYTES).set(new Uint8Array(encodeHeader({
      messageId, fragmentIndex: i, fragmentCount, totalPayloadLength
    })));
    new Uint8Array(frameBuf, HEADER_BYTES).set(slice);
    out[i] = frameBuf;
  }
  return out;
};

const DEFAULT_STALE_MS = 10000;
const DEFAULT_MAX_IN_FLIGHT = 256;

export class Reassembler {
  constructor({ staleMs = DEFAULT_STALE_MS, maxInFlight = DEFAULT_MAX_IN_FLIGHT } = {}) {
    this.staleMs = staleMs;
    this.maxInFlight = maxInFlight;
    this.sets = new Map();
  }

  sweep(now = Date.now()) {
    let evicted = 0;
    for (const [id, set] of this.sets) {
      if (now - set.lastSeen > this.staleMs) { this.sets.delete(id); evicted++; }
    }
    return evicted;
  }

  _evictOldestIfOverCap() {
    if (this.sets.size <= this.maxInFlight) return;
    let oldestId = null, oldestTs = Infinity;
    for (const [id, set] of this.sets) {
      if (set.lastSeen < oldestTs) { oldestTs = set.lastSeen; oldestId = id; }
    }
    if (oldestId !== null) this.sets.delete(oldestId);
  }

  feed(rawFragment) {
    const now = Date.now();
    this.sweep(now);
    const u8 = toUint8(rawFragment);
    const header = decodeHeader(u8);
    if (!header) return null;
    const { messageId, fragmentIndex, fragmentCount, totalPayloadLength } = header;
    if (fragmentCount < 1 || fragmentIndex >= fragmentCount) return null;

    let set = this.sets.get(messageId);
    if (!set) {
      set = { fragmentCount, totalPayloadLength, parts: new Map(), received: 0, lastSeen: now };
      this.sets.set(messageId, set);
      this._evictOldestIfOverCap();
    } else if (set.fragmentCount !== fragmentCount || set.totalPayloadLength !== totalPayloadLength) {

      set = { fragmentCount, totalPayloadLength, parts: new Map(), received: 0, lastSeen: now };
      this.sets.set(messageId, set);
    }

    set.lastSeen = now;
    if (!set.parts.has(fragmentIndex)) {
      set.parts.set(fragmentIndex, u8.subarray(HEADER_BYTES));
      set.received++;
    }

    if (set.received < set.fragmentCount) return null;

    const out = new Uint8Array(set.totalPayloadLength);
    let offset = 0;
    for (let i = 0; i < set.fragmentCount; i++) {
      const part = set.parts.get(i);
      out.set(part, offset);
      offset += part.byteLength;
    }
    this.sets.delete(messageId);
    return out;
  }

  pendingCount() { return this.sets.size; }
  has(messageId) { return this.sets.has(messageId); }
}

export const createReassembler = (opts) => new Reassembler(opts);
export { HEADER_BYTES };
