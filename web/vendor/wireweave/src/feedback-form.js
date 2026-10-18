export const mountFeedbackForm = ({
  container, feedback, pageId, metadata = {}, storage = null, title = 'Leave a message', threadId: initialThreadId = ''
} = {}) => {
  if (!container?.ownerDocument || !feedback || typeof pageId !== 'string' || !pageId.trim()) throw new Error('Feedback form: container, feedback and pageId required');
  const document = container.ownerDocument;
  let threadId = initialThreadId;
  let disposed = false;
  let busy = false;
  const storageKey = 'ww_feedback_thread:' + JSON.stringify([feedback.serverId, pageId]);
  if (!threadId) {
    try { threadId = storage?.getItem(storageKey) || ''; } catch {}
  }
  const element = (tag, value = '', attributes = {}) => {
    const node = document.createElement(tag);
    node.textContent = value;
    for (const [key, val] of Object.entries(attributes)) node.setAttribute(key, val);
    return node;
  };
  const root = element('section', '', { 'aria-label': title, class: 'ww-feedback' });
  root.append(element('h2', title), element('p', 'Messages, replies, contact details and metadata are public. Include only information you want to share.'));
  const form = element('form');
  const field = (label, tag, attributes) => {
    const wrapper = element('label', label);
    const input = element(tag, '', attributes);
    wrapper.append(input);
    form.append(wrapper);
    return { wrapper, input };
  };
  const name = field('Name (optional)', 'input', { name: 'name', type: 'text', maxlength: '120', autocomplete: 'nickname' });
  const contact = field('Public contact (optional)', 'input', { name: 'contact', type: 'text', maxlength: '320' });
  const message = field('Message', 'textarea', { name: 'message', required: '', maxlength: '10000', rows: '5' });
  const extra = field('Metadata as JSON (optional)', 'textarea', { name: 'metadata', rows: '3', maxlength: '16000' });
  extra.input.placeholder = '{"category":"bug","version":"1.0"}';
  const submit = element('button', 'Send message', { type: 'submit' });
  form.append(submit);
  const status = element('p', '', { role: 'status', 'aria-live': 'polite' });
  const history = element('div', '', { 'aria-label': 'Conversation' });
  const newMessage = element('button', 'Start a new message', { type: 'button' });
  newMessage.hidden = !threadId;
  const onNewMessage = () => {
    if (busy || disposed) return;
    threadId = '';
    try { storage?.removeItem(storageKey); } catch {}
    history.replaceChildren();
    name.wrapper.hidden = contact.wrapper.hidden = false;
    submit.textContent = 'Send message';
    newMessage.hidden = true;
    status.textContent = 'Start a separate conversation.';
  };
  newMessage.addEventListener('click', onNewMessage);
  root.append(history, form, newMessage, status);
  container.append(root);
  const renderThread = () => {
    const thread = threadId && feedback.get(threadId);
    if (!thread || disposed) return;
    history.replaceChildren();
    history.append(element('p', 'Reference: ' + thread.id), element('p', 'Status: ' + thread.status), element('p', thread.message));
    for (const reply of thread.replies) history.append(element('p', reply.author + ': ' + reply.message));
    name.wrapper.hidden = contact.wrapper.hidden = true;
    submit.textContent = 'Send follow-up';
    newMessage.hidden = false;
  };
  const onFeedback = () => {
    try { renderThread(); } catch (error) { if (!disposed) status.textContent = 'Could not load replies: ' + error.message; }
  };
  feedback.addEventListener('feedback', onFeedback);
  const stop = feedback.subscribe({ pageId });
  const hydration = new AbortController();
  const ready = feedback.fetchOnce({ ...(threadId ? { threadId } : { pageId }), signal: hydration.signal }).then(() => {
    renderThread();
    if (threadId && !feedback.get(threadId) && !disposed) status.textContent = 'Saved conversation is not available from the relays yet. Reload to try again.';
  }).catch(error => { if (!disposed) status.textContent = 'Could not load replies: ' + error.message; });
  const onSubmit = async event => {
    event.preventDefault();
    if (busy || disposed) return;
    busy = true;
    submit.disabled = true;
    status.textContent = 'Sending…';
    let accepted;
    try {
      const additional = extra.input.value.trim() ? JSON.parse(extra.input.value) : {};
      if (!additional || typeof additional !== 'object' || Array.isArray(additional)) throw new Error('Metadata must be a JSON object');
      const supplied = typeof metadata === 'function' ? metadata() : metadata;
      if (!supplied || typeof supplied !== 'object' || Array.isArray(supplied)) throw new Error('Page metadata must be a JSON object');
      if (!feedback.auth) throw new Error('A visitor signing identity is required');
      if (!feedback.auth.pubkey) feedback.auth.loadFromStorage() || feedback.auth.generateKey();
      const body = { message: message.input.value, metadata: { ...supplied, ...additional } };
      if (threadId) {
        await feedback.fetchOnce({ threadId, signal: hydration.signal });
        accepted = await feedback.reply(threadId, body);
      } else {
        accepted = await feedback.submit(pageId, { ...body, name: name.input.value, contact: contact.input.value });
        threadId = accepted.id;
        try { storage?.setItem(storageKey, threadId); } catch {}
        await feedback.fetchOnce({ threadId, signal: hydration.signal });
      }
      if (disposed) return;
      message.input.value = '';
      extra.input.value = '';
      status.textContent = 'Message accepted. Save reference ' + threadId + ' to follow up.';
      renderThread();
      root.dispatchEvent(new document.defaultView.CustomEvent('feedback-sent', { bubbles: true, detail: { threadId, eventId: accepted.id } }));
    } catch (error) {
      if (error.code === 'FEEDBACK_DELIVERY_UNCONFIRMED' && error.eventId && !threadId) {
        threadId = error.eventId;
        newMessage.hidden = false;
        try { storage?.setItem(storageKey, threadId); } catch {}
      }
      if (!disposed) status.textContent = (accepted ? 'Message accepted; could not load the conversation: ' : 'Message was not confirmed: ') + error.message + (threadId ? '. Reference: ' + threadId : '') + '. Keep your reference and check the conversation before retrying.';
    } finally {
      busy = false;
      if (!disposed) submit.disabled = false;
    }
  };
  form.addEventListener('submit', onSubmit);
  return {
    element: root, ready,
    get threadId() { return threadId; },
    destroy() {
      disposed = true;
      hydration.abort();
      form.removeEventListener('submit', onSubmit);
      newMessage.removeEventListener('click', onNewMessage);
      feedback.removeEventListener('feedback', onFeedback);
      stop();
      root.remove();
    }
  };
};
