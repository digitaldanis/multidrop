#!/usr/bin/env node
import process from 'node:process';
import WebSocket from 'ws';
import * as nostrTools from 'nostr-tools';
import { RelayPool } from './relay-pool.js';
import { NostrAuth } from './auth.js';
import { Roles } from './roles.js';
import { createFeedback } from './feedback.js';
import { createFeedbackTools } from './feedback-tools.js';

let pool;
let feedback;
let roles;
const serverId = process.env.WIREWEAVE_FEEDBACK_SERVER;
try {
  if (!serverId) throw new Error('WIREWEAVE_FEEDBACK_SERVER required');
  const relays = process.env.WIREWEAVE_FEEDBACK_RELAYS
    ? JSON.parse(process.env.WIREWEAVE_FEEDBACK_RELAYS)
    : ['wss://relay.damus.io', 'wss://nos.lol', 'wss://relay.primal.net'];
  if (!Array.isArray(relays) || !relays.length || relays.some(url => typeof url !== 'string' || !/^wss?:\/\//.test(url))) throw new Error('Relay configuration must be a nonempty JSON array of WebSocket URLs');
  const chunks = [];
  let inputBytes = 0;
  for await (const chunk of process.stdin) {
    inputBytes += chunk.length;
    if (inputBytes > 65536) throw new Error('Tool request exceeds 64 KiB');
    chunks.push(chunk);
  }
  const input = Buffer.concat(chunks).toString('utf8');
  const request = JSON.parse(input);
  pool = new RelayPool({ relays, verifyEvent: nostrTools.verifyEvent, WebSocketImpl: WebSocket });
  let auth = null;
  if (process.env.WIREWEAVE_FEEDBACK_KEY) {
    auth = new NostrAuth({ nostrTools });
    auth.importKey(process.env.WIREWEAVE_FEEDBACK_KEY);
    roles = new Roles({ relayPool: pool, auth });
  }
  feedback = createFeedback({ relayPool: pool, auth, roles, serverId });
  pool.connect();
  const result = await createFeedbackTools({ feedback }).callTool(request);
  process.stdout.write(JSON.stringify(result) + '\n');
  if (result.isError) process.exitCode = 1;
} catch (error) {
  process.stdout.write(JSON.stringify({ isError: true, content: [{ type: 'text', text: error.message }] }) + '\n');
  process.exitCode = 1;
} finally {
  feedback?.unsubscribe();
  roles?.unsubscribe(serverId);
  pool?.disconnect();
}
