// Builds workflow.json from format.js. Run: node build.mjs
// Node ids are fixed so a rebuild diffs cleanly and PUT keeps the canvas stable.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const formatSource = readFileSync(join(here, 'format.js'), 'utf8');

export const EVENTS = [
  'bloodbank.genesis.approval.requested',
  'bloodbank.genesis.approval.granted',
  'bloodbank.genesis.approval.denied',
  'bloodbank.genesis.approval.expired',
  'bloodbank.genesis.alert.raised',
];

export const workflow = {
  name: 'Genesis -> ntfy',
  nodes: [
    {
      id: '6c1f3a52-8a0b-4d3e-9c1e-0d6f7b9a1001',
      name: 'Genesis event',
      type: 'n8n-nodes-bloodbank.bloodbankTrigger',
      typeVersion: 1,
      position: [0, 0],
      parameters: {
        events: EVENTS,
        delivery: 'durable',
        acknowledge: 'afterExecution',
        catchUpHours: 24,
        connection: {},
      },
    },
    {
      id: '6c1f3a52-8a0b-4d3e-9c1e-0d6f7b9a1002',
      name: 'Format ntfy',
      type: 'n8n-nodes-base.code',
      typeVersion: 2,
      position: [300, 0],
      parameters: {
        mode: 'runOnceForEachItem',
        language: 'javaScript',
        jsCode: `${formatSource}\nreturn { json: { ntfy: formatNtfy($input.item.json) } };\n`,
      },
    },
    {
      id: '6c1f3a52-8a0b-4d3e-9c1e-0d6f7b9a1003',
      name: 'Publish to ntfy',
      type: 'n8n-nodes-base.httpRequest',
      typeVersion: 4.2,
      position: [600, 0],
      retryOnFail: true,
      maxTries: 3,
      waitBetweenTries: 2000,
      parameters: {
        method: 'POST',
        url: 'https://ntfy.delo.sh/',
        authentication: 'genericCredentialType',
        genericAuthType: 'httpHeaderAuth',
        sendBody: true,
        specifyBody: 'json',
        jsonBody: '={{ JSON.stringify($json.ntfy) }}',
        options: { timeout: 15000 },
      },
      credentials: {
        httpHeaderAuth: { id: 'Ek5QjwyMWTolNgcO', name: 'ntfy phone publisher (n8n user)' },
      },
    },
  ],
  connections: {
    'Genesis event': { main: [[{ node: 'Format ntfy', type: 'main', index: 0 }]] },
    'Format ntfy': { main: [[{ node: 'Publish to ntfy', type: 'main', index: 0 }]] },
  },
  settings: { executionOrder: 'v1', saveDataSuccessExecution: 'all' },
};

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(join(here, 'workflow.json'), JSON.stringify(workflow, null, 2) + '\n');
  console.log('wrote workflow.json');
}
