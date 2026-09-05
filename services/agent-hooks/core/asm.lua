-- asm.lua -- the atomic arbiter for the 33GOD Agent State Machine.
--
-- The hook process is only a PROPOSER: it sends one commutative signal and
-- forgets it. THIS is the serialization point. Redis is single-threaded and
-- EVAL is atomic, so N racing hooks from N panes need no owning process, no
-- CAS retry, no WATCH/MULTI, and cannot lose an update. The `from` in every
-- transition is provably the value that was replaced, because the compare and
-- the write are one indivisible operation.
--
-- Counter deltas are commutative and floored, so signal ARRIVAL ORDER does not
-- matter -- which is the property that lets three parallel Bash calls produce
-- exactly ONE working->tool_running edge instead of three.
--
-- Returns the transition JSON when the derived level CHANGED, else nil. A
-- no-op edge never escapes, so a side-effect handler cannot fire on a
-- self-transition.

local hkey  = KEYS[1]   -- asm:a:{scope}        HASH   current state
local tkey  = KEYS[2]   -- asm:t:{scope}        STREAM transition log
local lkey  = KEYS[3]   -- asm:lane:{scope}     ZSET   lane_id -> last_ms
local live  = KEYS[4]   -- asm:live             ZSET   scope -> last_ms
local pidx  = KEYS[5]   -- asm:idx:pane:{zs}:{p} STRING scope pointer

local sig        = ARGV[1]
local ttl        = tonumber(ARGV[2])
local lane       = ARGV[3]
local meta       = cjson.decode(ARGV[4])
local lane_grace = tonumber(ARGV[5])
local err_grace  = tonumber(ARGV[6])
local att_ms     = tonumber(ARGV[7])
local maxlen     = tonumber(ARGV[8])
local scope      = ARGV[9]

-- Server clock, so racing hooks from different processes share one timebase.
local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local cur = {}
local raw = redis.call('HGETALL', hkey)
for i = 1, #raw, 2 do cur[raw[i]] = raw[i + 1] end
local function num(k) return tonumber(cur[k]) or 0 end

-- Coerce anything into a value redis.call will accept.
--
-- NOT paranoia: cjson decodes JSON `null` to `cjson.null`, a LIGHTUSERDATA,
-- which is TRUTHY in Lua. So the obvious `meta.foo or ''` does NOT catch a
-- null -- it passes the userdata straight through and Redis aborts the whole
-- script with "arguments must be strings or integers", killing the state
-- write silently and fail-open. One null anywhere in meta would have taken
-- the entire machine down with no error anyone would ever see.
local function s(v)
  local t = type(v)
  if t == 'string' then return v end
  if t == 'number' then return string.format('%d', math.floor(v)) end
  return ''
end

local prev          = cur['state']
local turn          = num('turn')
local tools         = num('tools')
local blocked_until = num('blocked_until')
local err_ms        = num('err_ms')
local seq           = num('seq')

-- ---- which lane is 'main'? ------------------------------------------------
--
-- SELF-CALIBRATING, and it has to be. CLAUDE_CODE_SESSION_ID is set in EVERY
-- child process Claude Code spawns -- hook runners included, not just Task
-- subagents -- so treating "that env var is set" as "this is a subagent"
-- would pin every claude agent at `delegating` forever. Observed directly
-- while building this.
--
-- Instead: the FIRST lane this scope ever reports IS main. Anything different
-- afterwards is a genuine second lane. No env var is trusted, it works
-- identically for all five CLIs, and a fresh `start` recalibrates it.
local main_lane = cur['main_lane']
if main_lane == nil or main_lane == '' or sig == 'start' then
  main_lane = lane
  redis.call('HSET', hkey, 'main_lane', s(lane))
end
local is_sub = (lane ~= '' and lane ~= 'main' and lane ~= main_lane)

-- ---- fold: commutative counter deltas ------------------------------------
--
-- `quiesce` and `prompt` zero the counters UNCONDITIONALLY. That is not
-- tidiness, it is the escape hatch for codex, whose PostToolUse fires on ~2%
-- of tool requests (17/734 measured over 6h). Pairing tool_req against
-- tool_done alone would wedge every codex agent in tool_running forever; the
-- next turn boundary always repairs it.

if sig == 'start' then
  turn, tools, blocked_until, err_ms = 0, 0, 0, 0
  redis.call('DEL', lkey)
elseif sig == 'prompt' then
  turn, tools, blocked_until = 1, 0, 0
elseif sig == 'tool_req' then
  turn  = 1
  tools = tools + 1
elseif sig == 'tool_done' then
  tools = tools - 1
elseif sig == 'sub_start' then
  turn = 1
  if is_sub then redis.call('ZADD', lkey, now, lane) end
elseif sig == 'sub_done' then
  -- claude has NO invocation_start binding (9 bindings, SubagentStop only), so
  -- its SubagentStop arrives orphaned. ZREM of an absent member is a no-op and
  -- ZCARD floors at 0, so the orphan cannot underflow into a negative count.
  if is_sub then redis.call('ZREM', lkey, lane) end
elseif sig == 'quiesce' then
  turn, tools, blocked_until, err_ms = 0, 0, 0, 0
  redis.call('DEL', lkey)
elseif sig == 'attention' then
  blocked_until = now + att_ms
elseif sig == 'fail' then
  err_ms = now
end

if tools < 0  then tools = 0  end
if tools > 64 then tools = 64 end
if turn  < 0  then turn  = 0  end

-- A non-main lane seen recently means a subagent is live. Refresh on any
-- in-turn signal so a long-running subagent does not decay mid-flight.
if is_sub and (sig == 'tool_req' or sig == 'tool_done' or sig == 'prompt') then
  redis.call('ZADD', lkey, now, lane)
end
redis.call('ZREMRANGEBYSCORE', lkey, '-inf', now - lane_grace)
local subs = redis.call('ZCARD', lkey)

-- ---- derive: a pure function of (counters, freshness) ---------------------
-- Priority order is the whole semantics. awaiting_human outranks everything
-- because a blocked agent that is also mid-tool is blocked, not busy.
local level
if blocked_until > now then
  level = 'awaiting_human'
elseif err_ms > 0 and (now - err_ms) < err_grace then
  level = 'failed'
elseif subs > 0 then
  level = 'delegating'
elseif tools > 0 then
  level = 'tool_running'
elseif turn > 0 then
  level = 'working'
elseif sig == 'start' then
  level = 'starting'
else
  level = 'idle'
end

redis.call('HSET', hkey,
  'state', level, 'turn', s(turn), 'tools', s(tools), 'subs', s(subs),
  'blocked_until', s(blocked_until), 'err_ms', s(err_ms), 'last_ms', s(now),
  'scope', scope, 'sv', '1',
  'cli',            s(meta.cli),
  'pid',            s(meta.pid),
  'starttime',      s(meta.starttime),
  'cwd',            s(meta.cwd),
  'basis',          s(meta.basis),
  'zellij_session', s(meta.zellij_session),
  'zellij_pane',    s(meta.zellij_pane),
  'correlationid',  s(meta.correlationid),
  'session_id',     s(meta.session_id),
  'last_role',      s(meta.last_role))

-- Every key carries its own TTL. maxmemory-policy is `noeviction` with
-- maxmemory 0 on the box's ONLY Redis, shared with the live nanoleaf wall and
-- Holocene's tooling stats: nothing is ever evicted, so an unbounded key OOMs
-- the server and takes two unrelated systems down with it.
redis.call('EXPIRE', hkey, ttl)
redis.call('EXPIRE', lkey, ttl)
redis.call('ZADD', live, now, scope)
-- asm:live is the one SHARED key here, so it cannot inherit a per-scope TTL.
-- Bound it both ways instead: drop members older than the TTL window (dead
-- scopes whose hashes have already expired), and expire the key itself so an
-- idle box sheds it entirely. Without this it is the one key that grows
-- forever -- which on a maxmemory-policy=noeviction Redis shared with the
-- nanoleaf wall and Holocene's stats is an OOM, not a leak.
redis.call('ZREMRANGEBYSCORE', live, '-inf', now - (ttl * 1000))
redis.call('EXPIRE', live, ttl)
if pidx ~= '' then redis.call('SET', pidx, scope, 'EX', ttl) end

if prev == level then
  return nil
end

local since = num('since')
local held  = 0
if since > 0 then held = now - since end
seq = seq + 1

redis.call('HSET', hkey,
  'prev', prev or '', 'since', s(now), 'prev_held_ms', s(held), 'seq', s(seq))

local tr = {
  scope          = scope,
  from           = prev or 'none',
  to             = level,
  held_ms        = held,
  reason         = sig,
  cli            = s(meta.cli),
  pid            = s(meta.pid),
  cwd            = s(meta.cwd),
  zellij_session = s(meta.zellij_session),
  zellij_pane    = s(meta.zellij_pane),
  basis          = s(meta.basis),
  tools          = tools,
  subs           = subs,
  turn           = turn,
  seq            = seq,
  at_ms          = now,
}
local js = cjson.encode(tr)

-- MAXLEN *and* EXPIRE. MAXLEN alone leaves an unbounded number of dead-scope
-- streams; EXPIRE alone leaves one hot scope unbounded.
redis.call('XADD', tkey, 'MAXLEN', '~', maxlen, '*', 'j', js)
redis.call('EXPIRE', tkey, ttl)
redis.call('PUBLISH', 'asm:transitions', js)

return js
