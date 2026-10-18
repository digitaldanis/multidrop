const isQuotaError = (err) =>
  err && (err.name === 'QuotaExceededError' || err.name === 'NS_ERROR_DOM_QUOTA_REACHED' || err.code === 22 || err.code === 1014);

export function safeSetItem(storage, target, key, value) {
  if (!storage) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch (err) {
    if (isQuotaError(err)) {
      target?.dispatchEvent?.(new CustomEvent('storage-error', { detail: { key, reason: 'quota', message: 'Storage full — some data may not be saved' } }));
    } else {
      target?.dispatchEvent?.(new CustomEvent('storage-error', { detail: { key, reason: 'unknown', message: err?.message || String(err) } }));
    }
    return false;
  }
}
