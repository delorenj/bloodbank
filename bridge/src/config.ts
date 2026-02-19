export const config = {
  // OpenClaw session directories to watch
  sessionDirs: [
    `${process.env.HOME}/.openclaw/agents/main/sessions/`,
    `${process.env.HOME}/.openclaw/agents/work/sessions/`,
    `${process.env.HOME}/.openclaw/agents/family/sessions/`,
  ],

  // Agent ID → human name mapping
  agentNames: {
    main: 'cack',
    work: 'rererere',
    family: 'tonny',
  } as Record<string, string>,

  // Bloodbank API
  bloodbankUrl: 'http://localhost:8682',

  // Rate limiting (don't flood)
  maxEventsPerSecond: 20,

  // Only tail new lines (don't replay history on startup)
  tailOnly: true,

  // Truncate preview fields to this length
  maxPreviewLength: 200,
};
