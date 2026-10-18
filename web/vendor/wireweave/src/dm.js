const GIFT_WRAP_KIND = 1059;
const SEAL_KIND = 13;
const RUMOR_KIND = 14;
const TWO_DAYS = 2 * 24 * 60 * 60;
const HEX64 = /^[0-9a-f]{64}$/;

const randomPastTimestamp = () => Math.floor(Date.now() / 1000 - Math.random() * TWO_DAYS);

export class DMError extends Error {
    constructor(code, message) {
        super(message);
        this.name = 'DMError';
        this.code = code;
    }
}

export class DM extends EventTarget {
    constructor({ relayPool, auth, nostrTools }) {
        super();
        if (!relayPool || !auth || !nostrTools) throw new Error('DM: deps required');
        if (!nostrTools.nip44) throw new Error('nostr-tools nip44 missing');
        if (!nostrTools.nip59) throw new Error('nostr-tools nip59 missing');
        if (!nostrTools.getEventHash) throw new Error('nostr-tools getEventHash missing');
        this.pool = relayPool;
        this.auth = auth;
        this.NT = nostrTools;
        this.subId = null;
    }

    async send(peerPubkey, plaintext) {
        if (!this.auth.pubkey) throw new DMError('not-authenticated', 'Sign in before sending direct messages');
        if (!HEX64.test(peerPubkey || '')) throw new DMError('invalid-peer', 'Recipient public key is invalid');
        const createdAt = Math.floor(Date.now() / 1000);
        if (this.auth.privkey) {
            const rumor = { kind: RUMOR_KIND, created_at: createdAt, tags: [['p', peerPubkey]], content: plaintext };
            const wrapForPeer = this.NT.nip59.wrapEvent(rumor, this.auth.privkey, peerPubkey);
            const wrapForSelf = this.NT.nip59.wrapEvent(rumor, this.auth.privkey, this.auth.pubkey);
            this.pool.publish(wrapForPeer);
            this.pool.publish(wrapForSelf);
            return wrapForPeer;
        }
        const ext = this._extension();
        const rumor = this._rumor(plaintext, peerPubkey, createdAt);
        const wrapForPeer = await this._wrapWithExtension(ext, rumor, peerPubkey);
        const wrapForSelf = await this._wrapWithExtension(ext, rumor, this.auth.pubkey);
        this.pool.publish(wrapForPeer);
        this.pool.publish(wrapForSelf);
        return wrapForPeer;
    }

    async decrypt(wrap) {
        const rumor = await this.unwrap(wrap);
        return rumor.content;
    }

    async unwrap(wrap) {
        if (!this.auth.pubkey) throw new DMError('not-authenticated', 'Sign in before reading direct messages');
        if (this.auth.privkey) {
            try {
                return this.NT.nip59.unwrapEvent(wrap, this.auth.privkey);
            } catch (e) {
                throw new DMError('decrypt-failed', 'Could not decrypt this direct message: ' + e.message);
            }
        }
        const ext = this._extension();
        if (!this.NT.verifyEvent(wrap) || wrap.kind !== GIFT_WRAP_KIND) {
            throw new DMError('decrypt-failed', 'Direct message envelope failed verification');
        }
        let seal;
        let rumor;
        try {
            seal = JSON.parse(await ext.nip44.decrypt(wrap.pubkey, wrap.content));
        } catch (e) {
            throw new DMError('decrypt-failed', 'Could not decrypt this direct message: ' + e.message);
        }
        if (!seal || seal.kind !== SEAL_KIND || !this.NT.verifyEvent(seal)) {
            throw new DMError('decrypt-failed', 'Direct message seal failed verification');
        }
        try {
            rumor = JSON.parse(await ext.nip44.decrypt(seal.pubkey, seal.content));
        } catch (e) {
            throw new DMError('decrypt-failed', 'Could not decrypt this direct message: ' + e.message);
        }
        if (!rumor || rumor.kind !== RUMOR_KIND || rumor.pubkey !== seal.pubkey) {
            throw new DMError('decrypt-failed', 'Direct message sender does not match its seal');
        }
        return rumor;
    }

    subscribe(onMessage) {
        if (!this.auth.pubkey) throw new DMError('not-authenticated', 'DM: not authenticated');
        const subId = 'dm-' + this.auth.pubkey.slice(0, 16);
        this.subId = subId;
        this.pool.subscribe(subId, [
            { kinds: [GIFT_WRAP_KIND], '#p': [this.auth.pubkey] }
        ], (event) => {
            this.unwrap(event).then((rumor) => {
                const peer = rumor.pubkey === this.auth.pubkey
                    ? (rumor.tags.find(t => t[0] === 'p')?.[1] || '')
                    : rumor.pubkey;
                try {
                    onMessage({ event, rumor, plaintext: rumor.content, peer });
                } catch (e) {
                    this._emit('error', { event, code: 'handler-failed', error: e.message });
                }
            }, (e) => {
                this._emit('error', { event, code: e.code || 'decrypt-failed', error: e.message });
            });
        });
        return subId;
    }

    unsubscribe() {
        if (this.subId) { this.pool.unsubscribe(this.subId); this.subId = null; }
    }

    _extension() {
        const ext = this.auth.getExtension();
        if (!ext) throw new DMError('no-signer', 'Sign in with a key or a Nostr browser extension to use direct messages');
        if (typeof ext.nip44?.encrypt !== 'function' || typeof ext.nip44?.decrypt !== 'function' || typeof ext.signEvent !== 'function') {
            throw new DMError('nip44-unsupported', 'Your Nostr extension does not provide NIP-44 encryption, so direct messages are unavailable. Use an extension with NIP-44 support or sign in with a key.');
        }
        return ext;
    }

    _rumor(content, peerPubkey, createdAt) {
        const rumor = { kind: RUMOR_KIND, created_at: createdAt, tags: [['p', peerPubkey]], content, pubkey: this.auth.pubkey };
        rumor.id = this.NT.getEventHash(rumor);
        return rumor;
    }

    async _wrapWithExtension(ext, rumor, recipientPubkey) {
        const sealContent = await ext.nip44.encrypt(recipientPubkey, JSON.stringify(rumor));
        const seal = await ext.signEvent({ kind: SEAL_KIND, created_at: randomPastTimestamp(), tags: [], content: sealContent });
        if (!seal || seal.pubkey !== this.auth.pubkey || !this.NT.verifyEvent(seal)) {
            throw new DMError('signer-invalid', 'Extension returned a seal that does not verify as the logged-in key');
        }
        const ephemeralSk = this.NT.generateSecretKey();
        const conversationKey = this.NT.nip44.v2.utils.getConversationKey(ephemeralSk, recipientPubkey);
        const content = this.NT.nip44.v2.encrypt(JSON.stringify(seal), conversationKey);
        return this.NT.finalizeEvent({
            kind: GIFT_WRAP_KIND,
            created_at: randomPastTimestamp(),
            tags: [['p', recipientPubkey]],
            content
        }, ephemeralSk);
    }

    _emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
}

export const createDM = (opts) => new DM(opts);
