const test = require('node:test');
const assert = require('node:assert');
const Scope = require('../static/js/daily_vessel_scope.js');

const active = status => status === 'Open' || status === 'InProgress';
const risks = new Set(['COC & Flag', 'Urgent']);
const issue = (id, vesselId, over) => Object.assign({
  id, vessel_id: vesselId, vessel_name: `V${vesselId}`,
  issue_date: '2026-08-24', status: 'Open', priority: 'Normal',
}, over || {});

test('담당 로스터 밖 선박과 vessel_id 미지정 이슈는 목록에서 제외한다', () => {
  const groups = Scope.assignedGroups(
    [{ id: 1, name: 'Assigned', vessel_type: 'VLCC' }],
    [issue(10, 1), issue(20, 2), issue(30, null)], active, risks,
  );
  assert.deepEqual(groups.map(g => g.id), [1]);
  assert.deepEqual(groups[0].issues.map(i => i.id), [10]);
});

test('담당감독을 재지정해 로스터에 돌아오면 기존 이슈가 다시 표시된다', () => {
  const old = issue(20, 2, { status: 'InProgress', priority: 'Urgent' });
  const groups = Scope.assignedGroups(
    [{ id: 2, name: 'Reassigned', vessel_type: '' }], [old], active, risks,
  );
  assert.equal(groups.length, 1);
  assert.equal(groups[0].name, 'Reassigned');
  assert.equal(groups[0].active, 1);
  assert.equal(groups[0].risk, true);
});

test('현재 담당 선박은 이슈가 0건이어도 목록에 유지한다', () => {
  const groups = Scope.assignedGroups(
    [{ id: 3, name: 'No Issue', vessel_type: 'CNTR' }], [], active, risks,
  );
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].issues, []);
});
