const NAMESPACES = new Set(['ban', 'timeout', 'kick', 'page', 'channels', 'roles', 'settings', 'unban', 'mute']);

const PREFIX = 'zellous-';

export const dtag = (ns, ...parts) => {
  if (!NAMESPACES.has(ns)) throw new Error('dtag: unknown namespace ' + ns);
  return [PREFIX + ns, ...parts].join(':');
};

export const parseDtag = (s) => {
  if (typeof s !== 'string' || !s.startsWith(PREFIX)) return null;
  const parts = s.slice(PREFIX.length).split(':');
  const ns = parts.shift();
  if (!NAMESPACES.has(ns)) return null;
  return { ns, parts };
};

let lastReplaceableTs = 0;
export const replaceableTs = () => {
  lastReplaceableTs = Math.max(Math.floor(Date.now() / 1000), lastReplaceableTs + 1);
  return lastReplaceableTs;
};
