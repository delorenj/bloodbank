import { config } from './config';
import { startWatchers } from './watcher';

console.log('╔══════════════════════════════════════╗');
console.log('║   Bloodbank Bridge v1.0.0            ║');
console.log('║   OpenClaw → Bloodbank Event Bridge  ║');
console.log('╚══════════════════════════════════════╝');
console.log('');
console.log(`[bridge] Bloodbank URL: ${config.bloodbankUrl}`);
console.log(`[bridge] Rate limit: ${config.maxEventsPerSecond} events/sec`);
console.log(`[bridge] Tail-only: ${config.tailOnly}`);
console.log(`[bridge] Agent mappings:`);
for (const [id, name] of Object.entries(config.agentNames)) {
  console.log(`  ${id} → ${name}`);
}
console.log('');

startWatchers();
