const registry = new Map();

export const register = (key, obj) => {
  registry.set(key, obj);
  if (typeof window !== 'undefined') {
    window.__wireweave = window.__wireweave || {};
    window.__wireweave[key] = obj;
  }
};

export const deregister = (key) => {
  registry.delete(key);
  if (typeof window !== 'undefined' && window.__wireweave) {
    delete window.__wireweave[key];
  }
};

export const get = (key) => registry.get(key);

export const all = () => Object.fromEntries(registry);
