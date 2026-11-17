// Turns one bloodbank.genesis.* envelope into the JSON body ntfy publishes.
// Embedded verbatim into the workflow's "Format ntfy" Code node by build.mjs, so this file is
// the one place the wording lives. Plain function declarations only: n8n's Code node sandbox
// has no require() and the unit test loads the source through new Function().

var NTFY_TOPIC = 'genesis';
var NTFY_TZ = 'America/New_York';

function ntfyWhen(iso) {
  var t = new Date(iso);
  if (isNaN(t.getTime())) return String(iso || 'unknown');
  return t.toLocaleString('en-US', {
    timeZone: NTFY_TZ, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
  });
}

function ntfyClip(text, max) {
  var s = String(text == null ? '' : text);
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

function ntfyActionLines(kind, action) {
  var a = action && typeof action === 'object' ? action : {};
  var lines = [];
  if (kind === 'email.send' || a.to || a.subject) {
    if (Array.isArray(a.to) && a.to.length) lines.push('**To:** ' + a.to.join(', '));
    if (Array.isArray(a.cc) && a.cc.length) lines.push('**Cc:** ' + a.cc.join(', '));
    if (a.subject) lines.push('**Subject:** ' + a.subject);
    if (a.body_preview) {
      lines.push('');
      lines.push(String(a.body_preview).split('\n').map(function (l) { return '> ' + l; }).join('\n'));
    }
    return lines;
  }
  var keys = Object.keys(a);
  if (keys.length) lines.push('```\n' + ntfyClip(JSON.stringify(a, null, 1), 800) + '\n```');
  return lines;
}

function formatNtfy(e) {
  var d = (e && e.data) || {};
  var type = e && e.type;
  var test = d.test === true;
  var prefix = test ? '[test] ' : '';
  var who = d.client_name || d.client_id || 'unknown client';
  var base = {
    topic: NTFY_TOPIC,
    markdown: true,
    click: d.operator_url,
    actions: d.operator_url ? [{ action: 'view', label: 'Open operator', url: d.operator_url }] : [],
  };
  var tags = ['genesis'];
  if (test) tags.push('test_tube');

  if (type === 'bloodbank.genesis.approval.requested') {
    var lines = ['**' + ntfyClip(d.summary, 200) + '**', ''];
    if (d.question) lines.push(ntfyClip(d.question, 1500), '');
    var detail = ntfyActionLines(d.kind, d.action);
    if (detail.length) lines.push(detail.join('\n'), '');
    lines.push('Expires ' + ntfyWhen(d.expires_at) + ' · via ' + (d.channel || 'web') + ' · ' + (d.bot_id || ''));
    return Object.assign(base, {
      title: prefix + 'Approval needed · ' + who,
      message: ntfyClip(lines.join('\n'), 3500),
      priority: 4,
      tags: ['raised_hand'].concat(tags),
      sequence_id: d.approval_id,
    });
  }

  var resolved = {
    'bloodbank.genesis.approval.granted': ['Approved', 'white_check_mark'],
    'bloodbank.genesis.approval.denied': ['Declined', 'x'],
    'bloodbank.genesis.approval.expired': ['Expired', 'hourglass_flowing_sand'],
  }[type];
  if (resolved) {
    var via = d.decided_via ? ' (' + d.decided_via + ')' : '';
    return Object.assign(base, {
      title: prefix + resolved[0] + ' · ' + who,
      message: ntfyClip(resolved[0] + via + ': ' + (d.summary || d.kind || d.approval_id), 500),
      priority: 2,
      tags: [resolved[1]].concat(tags),
      sequence_id: d.approval_id,
    });
  }

  if (type === 'bloodbank.genesis.alert.raised') {
    var critical = d.severity === 'critical';
    var body = ['**' + String(d.reason || 'alert').replace(/_/g, ' ') + '**', '', ntfyClip(d.summary, 200)];
    if (d.detail) body.push('', ntfyClip(d.detail, 2000));
    if (d.bot_id) body.push('', d.bot_id);
    return Object.assign(base, {
      title: prefix + 'Genesis alert · ' + who,
      message: ntfyClip(body.join('\n'), 3500),
      priority: critical ? 5 : 4,
      tags: [critical ? 'sos' : 'rotating_light'].concat(tags),
      sequence_id: d.alert_id,
    });
  }

  throw new Error('Genesis -> ntfy does not handle event type ' + type);
}
