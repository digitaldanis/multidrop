export const createFSM = (xstate) => {
  if (!xstate?.createMachine) throw new Error('xstate required');
  return {
    voiceMachine: xstate.createMachine({
      initial: 'idle',
      states: {
        idle: { on: { connect: 'connecting' } },
        connecting: { on: { connected: 'connected', fail: 'idle' } },
        connected: { on: { disconnect: 'disconnecting' } },
        disconnecting: { on: { done: 'idle' } }
      }
    }),
    peerMachine: xstate.createMachine({
      initial: 'new',
      states: {
        new: { on: { offer: 'offering', recv_offer: 'answering' } },
        offering: { on: { recv_answer: 'connected', fail: 'new', restart: 'offering' } },
        answering: { on: { sent_answer: 'connected', recv_answer: 'connected', fail: 'new' } },
        connected: { on: { disconnect: 'reconnecting', close: 'closed' } },
        reconnecting: { on: { offer: 'offering', recv_answer: 'connected', close: 'closed' } },
        closed: {}
      }
    }),
    cameraMachine: xstate.createMachine({
      initial: 'idle',
      states: {
        idle: { on: { enable: 'requesting' } },
        requesting: { on: { enabled: 'active', denied: 'idle' } },
        active: { on: { disable: 'idle', error: 'idle' } },
        error: { on: { enable: 'requesting' } }
      }
    }),
    relayMachine: xstate.createMachine({
      initial: 'connecting',
      states: {
        connecting: { on: { connected: 'connected', fail: 'error' } },
        connected: { on: { fail: 'error', disconnect: 'disconnected' } },
        disconnected: { on: { reconnect: 'connecting' } },
        error: { on: { reconnect: 'connecting' } }
      }
    }),
    dataMachine: xstate.createMachine({
      initial: 'idle',
      states: {
        idle: { on: { connect: 'connecting' } },
        connecting: { on: { connected: 'connected', fail: 'idle' } },
        connected: { on: { disconnect: 'disconnecting', degrade: 'reconnecting' } },
        reconnecting: { on: { connected: 'connected', disconnect: 'disconnecting', fail: 'idle' } },
        disconnecting: { on: { done: 'idle' } }
      }
    }),
    sfuMachine: xstate.createMachine({
      initial: 'mesh',
      states: {
        mesh: { on: { elect: 'electing' } },
        electing: { on: { elected: 'star', dissolve: 'mesh' } },
        star: { on: { dissolve: 'mesh' } }
      }
    })
  };
};
