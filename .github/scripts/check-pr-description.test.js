'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const checkPrDescription = require('./check-pr-description');

function makeBody(linkedIssue, overrides = {}) {
  return [
    '## Summary',
    overrides.summary ?? 'This change documents and validates an isolated gate behavior.',
    '## Linked Issue',
    linkedIssue,
    '## Type of Change',
    overrides.type ?? '- [x] Bug fix',
    '## Checklist',
    overrides.checklist ?? '- [x] I searched existing issues and PRs.',
    '## How to Test',
    overrides.howTo ?? 'Run the focused Node test file and review the gate outcomes.',
  ].join('\n');
}

async function runGate(linkedIssue, overrides) {
  const state = { failures: [], comments: [], addedLabels: [], removedLabels: [] };
  const issues = {
    listComments: async () => ({ data: [] }),
    getLabel: async () => ({ data: { name: 'present' } }),
    addLabels: async (args) => state.addedLabels.push(args.labels[0]),
    removeLabel: async (args) => state.removedLabels.push(args.name),
    createComment: async (args) => state.comments.push(args.body),
    updateComment: async (args) => state.comments.push(args.body),
    deleteComment: async () => {},
  };
  const github = {
    rest: { issues },
    paginate: async () => [],
  };
  const context = {
    payload: {
      pull_request: {
        body: makeBody(linkedIssue, overrides),
        number: 17,
      },
    },
    repo: { owner: 'example', repo: 'odysseus' },
  };
  const core = {
    warning: () => {},
    setFailed: (message) => state.failures.push(message),
  };

  await checkPrDescription({ github, context, core });
  return state;
}

test('accepts existing GitHub issue reference forms', async (t) => {
  for (const reference of [
    '#63',
    'Fixes #63',
    'https://github.com/example/odysseus/issues/63',
  ]) {
    await t.test(reference, async () => {
      const state = await runGate(reference);
      assert.deepEqual(state.failures, []);
      assert.ok(state.addedLabels.includes('ready for review'));
    });
  }
});

test('accepts supported complete positive-integer Plane keys', async (t) => {
  for (const reference of ['PS-1', 'TMOS-104', 'EOT-256', 'EST-12']) {
    await t.test(reference, async () => {
      const state = await runGate(reference);
      assert.deepEqual(state.failures, []);
      assert.ok(state.addedLabels.includes('ready for review'));
    });
  }
});

test('accepts a description containing both GitHub and Plane references', async () => {
  const state = await runGate('Fixes #63; Plane item PS-1167');
  assert.deepEqual(state.failures, []);
});

test('rejects malformed, embedded, unsupported, and unrelated Plane references', async (t) => {
  const refusals = [
    ['zero', 'PS-0'],
    ['leading zero', 'PS-01'],
    ['empty number', 'TMOS-'],
    ['non-numeric suffix', 'EOT-12x'],
    ['embedded prefix', 'prefixPS-12'],
    ['embedded suffix', 'PS-12_suffix'],
    ['hyphenated suffix', 'EST-12-extra'],
    ['unsupported project', 'POS-12'],
    ['bare number', '12'],
    ['unrelated prose', 'See the relevant work item for context.'],
  ];
  for (const [name, reference] of refusals) {
    await t.test(name, async () => {
      const state = await runGate(reference);
      assert.equal(state.failures.length, 1);
      assert.match(state.comments[0], /\*\*Linked Issue\*\*/);
      assert.ok(state.addedLabels.includes('needs work'));
    });
  }
});

test('a valid Plane key does not bypass other description requirements', async (t) => {
  const cases = [
    ['summary', { summary: 'short' }, /\*\*Summary\*\*/],
    ['type of change', { type: '- [ ] Bug fix' }, /\*\*Type of Change\*\*/],
    ['duplicate search', { checklist: '- [ ] I searched existing issues and PRs.' }, /\*\*Checklist\*\*/],
    ['test detail', { howTo: 'tested locally' }, /\*\*How to Test\*\*/],
  ];
  for (const [name, overrides, expectedProblem] of cases) {
    await t.test(name, async () => {
      const state = await runGate('PS-1167', overrides);
      assert.equal(state.failures.length, 1);
      assert.match(state.comments[0], expectedProblem);
    });
  }
});
