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
local nothing
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
  -- Starting NEW work is unambiguous proof the human already unblocked us, so
  -- the attention window ends here. Without this, `awaiting_human` was sticky
  -- for the full 30 minutes: nothing but prompt/start/quiesce cleared it, so an
  -- agent that got a permission prompt, was approved, and carried on still read
  -- as blocked. Observed live at turn=1 tools=6.
  --
  -- Deliberately NOT cleared on tool_done: with parallel tools in flight, one
  -- COMPLETING says nothing about whether a permission prompt on another is
  -- still open. Completing old work is not evidence; starting new work is.
  blocked_until = 0
elseif sig == 'tool_done' then
  tools = tools - 1
elseif sig == 'sub_start' then
  turn = 1
  blocked_until = 0
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
elseif sig == 'discover' then
  -- The sweeper found a live agent process. Carries NO counter delta and never
  -- overwrites an observed state: it exists so a quiet agent stays in the table
  -- instead of ageing out of it. See the derive ladder below.
  nothing = true
elseif sig == 'stale' or sig == 'gone' then
  -- Sweeper verdicts. They carry no counter delta: the sweeper observed
  -- something the event stream cannot express (silence, or /proc vanishing),
  -- and the counters stay exactly as they were so a late signal self-heals
  -- back to the right level instead of resuming from a fiction.
  nothing = true
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
if sig == 'discover' then
  -- An agent we have only DISCOVERED is `unknown`, never `idle`. It would be
  -- very easy to call it idle -- an actively working agent fires hooks
  -- constantly, so silence really does suggest rest -- but that is an
  -- INFERENCE, and this machine's whole discipline is that an inference never
  -- wears an observation's clothes. Some CLIs in AGENT_COMMS have no hooks
  -- wired at all, and for those `idle` would simply be wrong.
  --
  -- On a row that already carries an observed state, keep it: this signal then
  -- does nothing but refresh the TTL and the liveness index, which is the
  -- entire point.
  if prev == nil or prev == '' then level = 'unknown' else level = prev end
elseif sig == 'gone' then
  -- A DIRECT OBSERVATION that the process is no longer in /proc, which is the
  -- one fact no bus consumer can ever learn. Terminal.
  level = 'gone'
elseif sig == 'stale' then
  level = 'stale'
elseif blocked_until > now then
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

-- A `discover` must never DOWNGRADE what a real hook already established.
-- The sweeper resolves identity from /proc and would otherwise overwrite a
-- richer observed basis (proc-env, agent-env) with the generic `discovered` on
-- every 15s tick. Live facts from /proc -- cwd, pane, pid -- are still
-- refreshed, because those genuinely can change.
local basis_out = s(meta.basis)
if sig == 'discover' and cur['basis'] ~= nil and cur['basis'] ~= ''
   and cur['basis'] ~= 'discovered' then
  basis_out = cur['basis']
end

redis.call('HSET', hkey,
  'state', level, 'turn', s(turn), 'tools', s(tools), 'subs', s(subs),
  'blocked_until', s(blocked_until), 'err_ms', s(err_ms), 'last_ms', s(now),
  'scope', scope, 'sv', '1',
  'cli',            s(meta.cli),
  'pid',            s(meta.pid),
  'starttime',      s(meta.starttime),
  'cwd',            s(meta.cwd),
  'basis',          basis_out,
  'zellij_session', s(meta.zellij_session),
  'zellij_pane',    s(meta.zellij_pane),
  'correlationid',  s(meta.correlationid),
  'session_id',     s(meta.session_id),
  'last_role',      s(meta.last_role),
  'profile',        s(meta.profile))

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

local function reap()
  redis.call('DEL', hkey, tkey, lkey)
  redis.call('ZREM', live, scope)
  if pidx ~= '' then redis.call('DEL', pidx) end
end

if prev == level then
  -- `gone` is terminal, so reap even on the (rare) repeat observation rather
  -- than leaving a dead row pinned in the table forever.
  if level == 'gone' then reap() end
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
  profile        = s(meta.profile),
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

-- Reap AFTER the edge has been recorded and published, so `->gone` still
-- reaches its handlers on the way out.
if level == 'gone' then reap() end

return js
