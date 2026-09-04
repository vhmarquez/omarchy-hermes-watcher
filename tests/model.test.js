const assert = require('node:assert/strict')
const Model = require('../Model.js')

const parsed = Model.parseSnapshot(JSON.stringify({
  schemaVersion: 1,
  activeBotCount: 2,
  activeTurnCount: 3,
  onlineBotCount: 3,
  onlineSessionCount: 4,
  onlineProfiles: [{ profile: 'default', sessionCount: 1, activeTurnCount: 0 }],
  availableProfiles: [{ profile: 'default', avatarUrl: 'file:///avatar.png?v=1-2' }],
  profiles: [{ profile: 'coder', activeTurnCount: 2, runningForSec: 61 }],
  recent: [{ eventId: 'done', profile: 'coder', state: 'succeeded', durationSec: 121 }],
  recentSessions: 'not-an-array',
  pendingNotifications: []
}))
assert.equal(parsed.ok, true)
assert.equal(parsed.activeBotCount, 2)
assert.equal(parsed.onlineBotCount, 3)
assert.equal(parsed.onlineSessionCount, 4)
assert.equal(parsed.onlineProfiles[0].profile, 'default')
assert.equal(parsed.availableProfiles[0].profile, 'default')
assert.equal(parsed.profiles[0].profile, 'coder')
assert.deepEqual(parsed.recentSessions, [])
assert.deepEqual(Model.emptySnapshot().recentSessions, [])
const cappedRecentSessions = Model.parseSnapshot(JSON.stringify({
  schemaVersion: 1,
  hermesRoot: '/tmp/hermes-root',
  recentSessions: Array.from({ length: 8 }, (_, index) => ({ sessionId: String(index) }))
}))
assert.equal(cappedRecentSessions.hermesRoot, '/tmp/hermes-root')
assert.deepEqual(cappedRecentSessions.recentSessions.map(x => x.sessionId), ['0', '1', '2', '3', '4', '5'])
assert.equal(Model.formatDuration(61), '1m 01s')
assert.equal(Model.formatDuration(3661), '1h 01m')
assert.equal(Model.formatRelative(940, 1000), '1m ago')
assert.equal(Model.formatRelative(999, 1000), 'just now')
assert.equal(Model.formatTokenCount(999), '999')
assert.equal(Model.formatTokenCount(186000), '186k')
assert.equal(Model.formatTokenCount(1250000), '1.3m')
assert.equal(Model.stateLabel('succeeded'), 'Succeeded')
assert.equal(Model.stateLabel('failed'), 'Failed')
assert.equal(Model.stateLabel('interrupted'), 'Interrupted')
assert.equal(Model.stateLabel('stale'), 'Stale')

const outcomes = [
  { eventId: 'ok', state: 'succeeded' },
  { eventId: 'bad', state: 'failed' },
  { eventId: 'stop', state: 'interrupted' },
  { eventId: 'lost', state: 'stale' }
]
assert.deepEqual(Model.filterRecent(outcomes, 'all').map(x => x.eventId), ['ok', 'bad', 'stop', 'lost'])
assert.deepEqual(Model.filterRecent(outcomes, 'success').map(x => x.eventId), ['ok'])
assert.deepEqual(Model.filterRecent(outcomes, 'issues').map(x => x.eventId), ['bad', 'stop', 'lost'])
assert.deepEqual(Model.filterRecent(null, 'all'), [])

assert.deepEqual(Model.notificationText(parsed.recent[0]), {
  title: 'coder finished',
  body: 'Completed in 2m 01s'
})

const malformed = Model.parseSnapshot('not json')
assert.equal(malformed.ok, false)
assert.match(malformed.lastError, /parse/i)

const empty = Model.parseSnapshot('   \n')
assert.equal(empty.ok, false)
assert.match(empty.lastError, /empty/i)
console.log('Model tests passed')
