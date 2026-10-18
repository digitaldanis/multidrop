const text = { type: 'string' };
const metadata = { type: 'object', additionalProperties: true };
const threadId = { type: 'string', pattern: '^[0-9a-f]{64}$' };
const schema = (properties, required = []) => ({ type: 'object', properties, required, additionalProperties: false });
export const feedbackToolDefinitions = [
  { name: 'feedback_list', description: 'Load public feedback from relays and list threads, including metadata and developer status.', inputSchema: schema({ pageId: text, requireAllRelays: { type: 'boolean' }, status: { enum: ['open', 'in_progress', 'resolved', 'closed'] }, assignee: text, limit: { type: 'integer', minimum: 1, maximum: 500 } }) },
  { name: 'feedback_get', description: 'Load a public feedback thread, its metadata, replies and triage history.', inputSchema: schema({ threadId, requireAllRelays: { type: 'boolean' } }, ['threadId']) },
  { name: 'feedback_submit', description: 'Publish public page feedback. All supplied content and metadata are public.', inputSchema: schema({ pageId: text, message: text, metadata, name: text, contact: text }, ['pageId', 'message']) },
  { name: 'feedback_reply', description: 'Reply as the original visitor or an authorized developer. The reply is public.', inputSchema: schema({ threadId, message: text, metadata }, ['threadId', 'message']) },
  { name: 'feedback_update', description: 'Authorized developers can triage, assign and resolve public feedback.', inputSchema: schema({ threadId, status: { enum: ['open', 'in_progress', 'resolved', 'closed'] }, assignee: text, metadata }, ['threadId']) }
];
const validateArguments = (definition, args) => {
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('Tool arguments must be an object');
  for (const key of Object.keys(args)) {
    const property = definition.inputSchema.properties[key];
    if (!property) throw new Error('Unknown argument: ' + key);
    const value = args[key];
    if (property.enum && !property.enum.includes(value)) throw new Error('Invalid ' + key);
    if (property.type === 'boolean' && typeof value !== 'boolean') throw new Error(key + ' must be a boolean');
    if (property.type === 'string' && typeof value !== 'string') throw new Error(key + ' must be a string');
    if (property.type === 'object' && (!value || typeof value !== 'object' || Array.isArray(value))) throw new Error(key + ' must be an object');
    if (property.type === 'integer' && (!Number.isInteger(value) || value < property.minimum || value > property.maximum)) throw new Error('Invalid ' + key);
    if (property.pattern && !new RegExp(property.pattern).test(value)) throw new Error('Invalid ' + key);
  }
  for (const key of definition.inputSchema.required) if (!Object.hasOwn(args, key)) throw new Error('Missing argument: ' + key);
};
export const createFeedbackTools = ({ feedback } = {}) => {
  if (!feedback) throw new Error('Feedback tools: feedback required');
  const call = async (name, args = {}) => {
    const definition = feedbackToolDefinitions.find(tool => tool.name === name);
    if (!definition) throw new Error('Unknown feedback tool: ' + name);
    validateArguments(definition, args);
    if (name === 'feedback_list') {
      const { requireAllRelays = false, ...filters } = args;
      await feedback.fetchOnce({ pageId: filters.pageId, requireAllRelays });
      return feedback.list(filters);
    }
    if (name === 'feedback_get') {
      await feedback.fetchOnce({ threadId: args.threadId, requireAllRelays: args.requireAllRelays || false });
      return feedback.get(args.threadId);
    }
    if (name === 'feedback_submit') {
      const { pageId, ...body } = args;
      const event = await feedback.submit(pageId, body);
      try {
        await feedback.fetchOnce({ threadId: event.id });
        return feedback.get(event.id);
      } catch (error) {
        error.eventId = event.id;
        error.threadId = event.id;
        error.accepted = true;
        error.code = 'FEEDBACK_ACCEPTED_HISTORY_UNAVAILABLE';
        throw error;
      }
    }
    const { threadId, ...body } = args;
    await feedback.fetchOnce({ threadId });
    const event = name === 'feedback_reply' ? await feedback.reply(threadId, body) : await feedback.update(threadId, body);
    try { return feedback.get(threadId); }
    catch (error) {
      error.eventId = event.id;
      error.threadId = threadId;
      error.accepted = true;
      error.code = 'FEEDBACK_ACCEPTED_HISTORY_UNAVAILABLE';
      throw error;
    }
  };
  return {
    definitions: structuredClone(feedbackToolDefinitions),
    call,
    async callTool({ name, arguments: args = {} } = {}) {
      try { const result = await call(name, args); return { content: [{ type: 'text', text: JSON.stringify(result) }], structuredContent: { result, history: feedback.historyStatus || null } }; }
      catch (error) { return { isError: true, content: [{ type: 'text', text: (error.accepted ? 'Message accepted; history unavailable: ' : '') + error.message + (error.eventId ? ' Reference: ' + error.eventId : '') }], structuredContent: { error: { message: error.message, code: error.code || 'FEEDBACK_ERROR', eventId: error.eventId || null, threadId: error.threadId || null, accepted: error.accepted || false } } }; }
    }
  };
};
