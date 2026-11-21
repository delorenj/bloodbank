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
const fixture = {
  key: 'BB60FIX-1', id: '11111111-2222-4333-8444-555555555555', title: 'Neutral synthetic task',
  labels: ['lifecycle:triaged'], stateName: 'Todo',
};
const scope = {
  repo: 'bb60-synthetic', boardId: '11111111-2222-4333-8444-555555555558', workspace: 'bb60-synthetic',
};
const route = { agentId: 'bb60-synthetic-worker', projectPath: '/disposable/rehearsal' };
const ids = {
  dispatchId: '11111111-2222-4333-8444-555555555556',
  commandId: '11111111-2222-4333-8444-555555555557',
  observedAt: '2026-11-21T00:00:00Z',
};
const facts = fleet.ticketFactsFromEnvelope({ data: {
  repo: scope.repo, board_id: scope.boardId, workspace: scope.workspace,
  ticket_key: fixture.key, ticket_id: fixture.id, title: fixture.title, labels: fixture.labels,
} });

function position(prompt, text) {
  const at = prompt.indexOf(text);
  assert.ok(at >= 0, `missing contract text: ${text}`);
  return at;
}

function section(prompt, start, end) {
  const from = position(prompt, start);
  const to = position(prompt, end);
  assert.ok(from < to, `${start} must precede ${end}`);
  return prompt.slice(from, to);
}

function assertAuthorityContract(prompt) {
  const selection = section(prompt, '  2. Resolve publication authority', '  3. Do the work');
  const commonSafety = section(selection, 'Shared-checkout safeguards', '     - Local-only:');
  const localSelection = section(selection, '     - Local-only:', '     - Ordinary publication:');
  const ordinarySelection = selection.slice(position(selection, '     - Ordinary publication:'));
  const execution = section(prompt, '  3. Do the work', '  4. Hand back with evidence');
  const localExecution = section(execution, '     - Local-only checkpoint:', '     - Authorized ordinary publication:');
  const ordinaryExecution = execution.slice(position(execution, '     - Authorized ordinary publication:'));

  assert.match(selection, /execution lane before any implementation, edits or commits/);
  assert.match(selection, /current description and latest comments/);
  assert.match(selection, /read any owner policy explicitly linked by the live ticket/);
  assert.match(selection, /Do not infer authority from its title, labels, the dispatch event, or a remembered instruction/);
  assert.match(selection, /Standing merge approval is not itself a publication grant/);
  assert.match(selection, /live read fails, or authority is missing, ambiguous, incomplete or conflicting, stop before edits or commits; do not push, merge or land/);
  assert.match(selection, /Hold with `lifecycle:needs-input` and ask one concrete question/);
  assert.match(selection, /unless `lifecycle:human` is present, in which case write nothing/);
  assert.match(selection, /Do not ask to lift an explicit restriction/);
  assert.match(commonSafety, /apply to EVERY route: never stash, autostash, reset, rebase over, or check out files you did not write, and never switch its branch/);
  assert.match(commonSafety, /Never move or discard another agent's changes/);
  assert.match(localSelection, /explicitly requires local-only \/ no push \/ no merge \/ no landing, select an isolated worktree before edits/);
  assert.match(localSelection, /Reuse an existing recovery worktree and branch when the live ticket explicitly authorizes that lane/);
  assert.match(localSelection, /cannot be used safely, hold rather than replace or alter it/);
  assert.match(ordinarySelection, /only when the current live ticket or an owner policy explicitly linked by it affirmatively authorizes ordinary publication, with no conflicting restriction/);
  assert.match(ordinarySelection, /Absence of a restriction is not permission/);
  assert.match(ordinarySelection, /isolated worktree before edits/);
  assert.match(execution, /only in the selected authorized lane/);
  assert.match(execution, /Leave the assignee empty/);
  assert.match(execution, /git add -- <paths>` and `git commit --only -- <paths>/);
  assert.match(execution, /never `git add -A` or a bare `git commit`/);
  assert.match(execution, /required checks and obtain the required independent reviews for that exact candidate/);
  assert.match(localExecution, /worktree path, branch and commit with check and independent-review evidence/);
  assert.match(localExecution, /Do not push any branch, merge or land on main, delete the checkpoint, or run `git unpushed` as a publication gate/);
  assert.match(localExecution, /Keep the checkpoint even on hold/);
  assert.match(localExecution, /Review and handback do not grant publication permission/);
  assert.match(ordinaryExecution, /land before handback/);
  assert.match(ordinaryExecution, /standing approval for ordinary merges.*only after the affirmative publication grant above/);
  assert.match(ordinaryExecution, /git pull --no-rebase origin main`, then push to main/);
  assert.match(ordinaryExecution, /conflicts in a file you did not write, abort it and hold with `lifecycle:blocked` naming the file/);
  assert.match(ordinaryExecution, /remove the worktree and delete the branch/);
  assert.match(ordinaryExecution, /only within the live publication grant/);
  assert.match(ordinaryExecution, /Every other repo.*needs its own publication authority/);
  assert.match(ordinaryExecution, /`git unpushed`.*authorized paths only/);
  assert.match(prompt, /local worktree path, branch and commit instead, include required-check and independent-review results, and state that nothing was pushed or merged/);
  assert.doesNotMatch(prompt, /Otherwise, when the live ticket is readable|Always land on main|Never leave a commit, a dirty file or a worktree only on this machine/);
}

function syntheticDispatch(run, ticket, review) {
  return run.dispatchCommand({ fleet }, {
    scope: { ...scope, review }, ticket, route, inProgressName: 'In Progress', ...ids,
  });
}

test('delegation source contract fails closed and selects isolation before implementation, with universal safeguards', () => {
  assertAuthorityContract(fleet.delegationPrompt(facts, route.projectPath));
});

test('actual Krebs dispatchCommand carries the authority contract and review/no-review handbacks (not worker behavior proof)', async (t) => {
  if (!existsSync(krebsPath)) return t.skip('Krebs sibling checkout is absent; actual-entrypoint proof remains required');
  const run = await import(pathToFileURL(krebsPath).href);
  for (const review of [true, false]) {
    const command = syntheticDispatch(run, fixture, review);
    const prompt = command.data.prompt;
    assert.equal(command.data.context.review_required, review);
    assert.equal(command.data.context.reason, 'ticket-delegation');
    assertAuthorityContract(prompt);
    assert.match(prompt, /re-read the ticket live from Plane/);
    assert.match(prompt, /`lifecycle:human`.*stop, write nothing/s);
    assert.match(prompt, /already carries `lifecycle:review`, stop/);
    for (const hold of ['lifecycle:needs-input', 'lifecycle:blocked', 'agent:failed', 'lifecycle:human']) {
      assert.ok(prompt.includes(`\`${hold}\``));
    }
    assert.match(prompt, review ? /scope REQUIRES review.*Do not close it/ : /scope has NO review step.*closing the ticket Done/);

    const triage = run.triageCommand({ fleet }, { scope, ticket: fixture, route, todoName: 'Todo', ...ids });
    assert.match(triage.data.prompt, /do not start or work the task/);
    assert.match(triage.data.prompt, /Never change the ticket's state/);
    assert.match(triage.data.prompt, /LAST thing you do/);
    const verdict = run.reviewCommand({ fleet }, { scope, ticket: fixture, route, ...ids });
    assert.match(verdict.data.prompt, /reviewer is never the implementer/);
    assert.match(verdict.data.prompt, /independent review worker/);
    assert.match(verdict.data.prompt, /no longer carries `lifecycle:review`.*stop and change nothing/s);
  }
});

test('Krebs title and dispatch-payload authority invariance is characterization only; a live worker must decide', async (t) => {
  if (!existsSync(krebsPath)) return t.skip('Krebs sibling checkout is absent; actual-entrypoint proof remains required');
  const run = await import(pathToFileURL(krebsPath).href);
  for (const review of [true, false]) {
    const baseline = syntheticDispatch(run, fixture, review).data.prompt;
    for (const title of ['TOLT-186 local only', 'TOLT-187 no remote publication', 'Ordinary task']) {
      const prompt = syntheticDispatch(run, { ...fixture, title }, review).data.prompt;
      assert.equal(prompt.replace(JSON.stringify(title), JSON.stringify(fixture.title)), baseline,
        'a title must not select publication authority');
    }
    // dispatchCommand intentionally does not forward descriptions/comments. These
    // inputs do not exercise live authority decisions; disposable workers must.
    for (const authority of [
      { description: 'Ordinary publication on main is authorized.', comments: [] },
      { description: 'Local checkpoint only. No push, merge or landing.', comments: [] },
      { description: '', comments: [] },
      { description: 'Authority is unresolved.', comments: [{ text: 'Ask before publication.' }] },
      { description: 'Publication authorized.', comments: [{ text: 'No push or landing.' }] },
    ]) {
      assert.equal(syntheticDispatch(run, { ...fixture, ...authority }, review).data.prompt, baseline,
        'payload descriptions are not a live Plane read or a worker execution');
    }
  }
});
