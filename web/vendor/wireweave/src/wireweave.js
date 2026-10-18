import { createRelayPool } from './relay-pool.js';
import { createAuth } from './auth.js';
import { createFSM } from './fsm.js';
import { createVoiceSession } from './voice.js';
import { createChat } from './chat.js';
import { createChannels } from './channels.js';
import { createServers } from './servers.js';
import { createMessageBus } from './message.js';
import { createBans } from './bans.js';
import { createRoles } from './roles.js';
import { createSettings } from './settings.js';
import { createMedia } from './media.js';
import { createPages } from './pages.js';
import { createDM } from './dm.js';
import { createDataSession } from './data.js';
import { createReactions } from './reactions.js';
import { createMutes } from './mutes.js';
import { createUnread } from './unread.js';
import { createForum } from './forum.js';
import { register } from './debug.js';
import { createFeedback } from './feedback.js';

export const createWireweave = ({
  nostrTools,
  xstate,
  storage = (typeof localStorage !== 'undefined' ? localStorage : null),
  extension = (typeof window !== 'undefined' ? window.nostr : null),
  relays = ['wss://relay.damus.io', 'wss://relay.primal.net', 'wss://nos.lol', 'wss://relay.snort.social', 'wss://relay.nostr.band', 'wss://nostr.wine', 'wss://offchain.pub'],
  mediaDevices = (typeof navigator !== 'undefined' ? navigator.mediaDevices : null),
  WebSocketImpl = (typeof WebSocket !== 'undefined' ? WebSocket : null)
} = {}) => {
  if (!nostrTools) throw new Error('wireweave: nostrTools required');
  if (!xstate) throw new Error('wireweave: xstate required');
  if (!storage) throw new Error('wireweave: storage required (no localStorage in this env — pass a {getItem,setItem,removeItem} adapter)');

  const fsm = createFSM(xstate);
  const pool = createRelayPool({ relays, verifyEvent: nostrTools.verifyEvent, WebSocketImpl });
  const auth = createAuth({ nostrTools, storage, extension });
  const message = createMessageBus();
  const roles = createRoles({ relayPool: pool, auth });
  const bans = createBans({ relayPool: pool, auth, roles });
  const settings = createSettings({ relayPool: pool, auth, roles });
  const pages = createPages({ relayPool: pool, auth, roles });
  const media = createMedia({ relayPool: pool, auth });
  const channels = createChannels({ relayPool: pool, auth });
  const reactions = createReactions({ relayPool: pool, auth });
  const mutes = createMutes({ relayPool: pool, auth });
  mutes.load();
  const unread = createUnread({ relayPool: pool, auth, storage });
  const forum = createForum({ relayPool: pool, auth });

  let currentChannelId = null;
  const chat = createChat({
    relayPool: pool, auth,
    getChannelContext: () => ({
      channelId: currentChannelId, serverId: servers.currentServerId || '',
      channelType: channels.channels.find((c) => c.id === currentChannelId)?.type || null
    }),
    isAdmin: (sid) => roles.isAdmin(sid),
    isRoleOf: (sid, pk) => roles.getRole(sid, pk),
    bans, mutes,
    getEventHash: nostrTools.getEventHash
  });

  const servers = createServers({
    relayPool: pool, auth, storage,
    onSwitch: async (serverId) => {
      roles.subscribe(serverId);
      settings.subscribe(serverId);
      bans.subscribe(serverId);
      pages.subscribe(serverId);
      if (serverId) {
        await new Promise((resolve) => {
          let done = false;
          const finish = () => { if (done) return; done = true; resolve(); };
          channels.load(serverId, finish);
          setTimeout(finish, 3000);
        });
      }
      const firstText = channels.channels.find(c => c.type === 'text');
      unread.start(serverId, channels.channels);
      if (firstText) {
        currentChannelId = firstText.id;
        unread.setActive(firstText.id);
        chat.loadHistory(firstText.id);
      }
    }
  });

  let voice = null;
  const ensureVoice = ({ serverId, displayName, onAudioTrack, onVideoTrack }) => {
    if (!voice) voice = createVoiceSession({ fsm, xstate, relayPool: pool, auth, mediaDevices, bans, serverId, onAudioTrack, onVideoTrack });
    else voice.serverId = serverId;
    return voice;
  };

  const setCurrentChannel = (id) => { currentChannelId = id; unread.setActive(id); return id ? chat.loadHistory(id) : Promise.resolve(); };



  let dm = null;
  const ensureDM = () => {
    if (!dm) dm = createDM({ relayPool: pool, auth, nostrTools });
    return dm;
  };




  let data = null;
  const ensureData = ({ room = '', displayName = 'Guest', namespace = '' } = {}) => {
    if (!data) data = createDataSession({ fsm, xstate, relayPool: pool, auth, namespace });
    return data;
  };

  const feedbackInboxes = new Map();
  const ensureFeedback = ({ serverId = servers.currentServerId, timeoutMs = 8000, maxEvents = 2000 } = {}) => {
    if (!serverId) throw new Error('Feedback serverId required');
    if (!feedbackInboxes.has(serverId)) feedbackInboxes.set(serverId, createFeedback({ relayPool: pool, auth, roles, serverId, timeoutMs, maxEvents }));
    return feedbackInboxes.get(serverId);
  };

  const api = {
    pool, auth, fsm, message, bans, roles, settings, pages, media, channels, servers, chat, reactions, mutes, unread, forum,
    get voice() { return voice; },
    ensureVoice,
    get dm() { return dm; },
    ensureDM,
    get data() { return data; },
    ensureData,
    ensureFeedback,
    setCurrentChannel,
    get currentChannelId() { return currentChannelId; },
    get currentServerId() { return servers.currentServerId; }
  };

  register('wireweave', api);
  return api;
};
