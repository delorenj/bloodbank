const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { existsSync } = require('node:fs');
const { dirname, resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const test = require('node:test');

const fleet = require('../dist/fleet.js');
const commonDir = execFileSync('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'], {
  cwd: __dirname, encoding: 'utf8',
}).trim();
const krebsPath = resolve(dirname(commonDir), '..', 'krebs/tick/run.mjs');
const krebsRun = pathToFileURL(krebsPath).href;

function liveDispatch(run, title, review) {
  return run.dispatchCommand({ fleet }, {
    scope: { repo: 'tolt', boardId: 'eb302959-0eac-48da-8fd5-ae1666bd500f', workspace: '33god', review },
    ticket: { key: 'TOLT-186', id: '11111111-2222-4333-8444-555555555555', title,
      labels: ['lifecycle:triaged'], stateName: 'Todo' },
    route: { agentId: 'tolt-pm', projectPath: '/disposable/rehearsal' },
    inProgressName: 'In Progress',
    dispatchId: '11111111-2222-4333-8444-555555555556',
    commandId: '11111111-2222-4333-8444-555555555557',
    observedAt: '2026-11-21T00:00:00Z',
  });
}

test('the actual Krebs dispatchCommand uses built Fleet authority and keeps review/no-review handbacks', async (t) => {
  if (!existsSync(krebsPath)) return t.skip('Krebs sibling checkout is absent; integration proof is required on the 33GOD host');
  const run = await import(krebsRun);
  for (const review of [true, false]) {
    const prompts = [];
    for (const title of ['TOLT-186 local only', 'TOLT-187 no remote publication', 'Ordinary task']) {
      const command = liveDispatch(run, title, review);
      prompts.push(command.data.prompt);
      assert.equal(command.data.context.review_required, review);
      assert.equal(command.data.context.reason, 'ticket-delegation');
      assert.match(command.data.prompt, /re-read the ticket live from Plane/);
      assert.match(command.data.prompt, /current description and latest comments/);
      assert.match(command.data.prompt, /Do not infer authority from its title, labels, the dispatch event/);
      assert.match(command.data.prompt, /explicit publication instructions conflict or are incomplete, do not push, merge or land/);
      assert.match(command.data.prompt, /hold with `lifecycle:needs-input`/);
      assert.match(command.data.prompt, /explicitly requires local-only \/ no push \/ no merge \/ no landing/);
      assert.match(command.data.prompt, /isolated worktree/);
      assert.match(command.data.prompt, /do not push any branch, merge or land on main/);
      assert.match(command.data.prompt, /Jarad gave standing approval for ordinary merges/);
      assert.match(command.data.prompt, /git pull --no-rebase origin main/);
      assert.match(command.data.prompt, /git add -- <paths>` and `git commit --only -- <paths>/);
      assert.match(command.data.prompt, /never stash, autostash/);
      assert.match(command.data.prompt, /`lifecycle:human`.*stop, write nothing/s);
      assert.match(command.data.prompt, /already carries `lifecycle:review`, stop/);
      assert.match(command.data.prompt, /link the landed commit; for an authorized local-only checkpoint/);
      assert.doesNotMatch(command.data.prompt, /Always land on main|Never leave a commit, a dirty file or a worktree only on this machine/);
      assert.match(command.data.prompt, review ? /scope REQUIRES review.*Do not close it/ : /scope has NO review step.*closing the ticket Done/);
    }
    assert.equal(prompts[0].replace('TOLT-186 local only', 'TOLT-187 no remote publication'), prompts[1], 'title does not select authority');
    assert.equal(prompts[0].replace('TOLT-186 local only', 'Ordinary task'), prompts[2], 'ordinary and restricted titles get the same authority gate');
  }
});
