(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DailyVesselScope = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // Daily 목록은 현재 감독-선박 연결표를 표시 정본으로 사용한다.
  // 과거 이슈가 남아 있어도 roster 밖 선박은 숨기고, 재연결되면 같은 이슈가 다시 합쳐진다.
  function assignedGroups(vessels, issues, isActiveStatus, riskPriorities) {
    const byId = new Map();
    for (const v of (vessels || [])) {
      byId.set(String(v.id), {
        id: v.id, name: v.name, type: v.vessel_type || '',
        issues: [], active: 0, risk: false, latest: '', daily_new_date: v.daily_new_date || null,
      });
    }
    for (const issue of (issues || [])) {
      if (issue.vessel_id == null) continue;
      const group = byId.get(String(issue.vessel_id));
      if (!group) continue;
      group.issues.push(issue);
      if (isActiveStatus(issue.status)) {
        group.active += 1;
        if (riskPriorities.has(issue.priority)) group.risk = true;
      }
      if ((issue.issue_date || '') > group.latest) group.latest = issue.issue_date || '';
    }
    return [...byId.values()];
  }

  function hasNewToday(vessel, now = new Date()) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(now);
    const value = type => parts.find(p => p.type === type).value;
    return vessel.daily_new_date === `${value('year')}-${value('month')}-${value('day')}`;
  }

  return { assignedGroups, hasNewToday };
}));
