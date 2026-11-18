-- statekit_prelude.lua -- shared conventions for 33GOD Redis state machines.
--
-- Prepended (by core/statekit.py) to every arbiter body: asm.lua (an EVAL
-- script) and crank.lua (a Redis Functions library). It defines ONE local
-- table, `sk`, and touches no key at load time, so it is legal at the top of a
-- Functions library (whose top level may not call redis.call) and costs a
-- script nothing it does not use.
--
-- The pattern every machine shares, and the reason this file exists:
--   * one HASH per subject holds the resolved current state (single writer:
--     the Lua arbiter; proposers send signals and never write it directly);
--   * one STREAM per subject is its transition log, capped (MAXLEN ~);
--   * one PUBSUB channel per machine announces every edge as JSON;
--   * a TTL on the hash garbage-collects a subject nobody re-asserts;
--   * a HEARTBEAT hash (ts + fields, optional TTL) says the observer that
--     re-asserts state is alive, so a reader can tell "off" from "unknown".
-- Only edges escape: a signal that changes nothing returns nil and writes no
-- transition, so a side-effect consumer never fires on a self-transition.

local sk = {}

-- Server clock in epoch ms, so racing proposers from different processes share
-- one timebase.
function sk.now_ms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

-- Coerce anything into a value redis.call accepts. cjson decodes JSON null to
-- cjson.null, a TRUTHY lightuserdata, so `v or ''` does not catch it and one
-- null would abort the whole script ("arguments must be strings or integers").
function sk.str(v)
  local t = type(v)
  if t == 'string' then return v end
  if t == 'number' then
    -- NaN and +-inf have no JSON/Redis spelling ('%d' of inf is garbage).
    if v ~= v or v == math.huge or v == -math.huge then return '' end
    -- An integral double is exact up to 2^53; '%d' prints it as an integer.
    if v == math.floor(v) and math.abs(v) < 9007199254740992 then
      return string.format('%d', v)
    end
    -- Everything else round-trips through %.17g (tostring's %.14g does not).
    return string.format('%.17g', v)
  end
  if t == 'boolean' then return v and '1' or '0' end
  return ''
end

-- A JSON value that is really there (not nil, not cjson.null).
function sk.present(v)
  return v ~= nil and v ~= cjson.null
end

-- HGETALL as a Lua table.
function sk.hgetall(key)
  local out, raw = {}, redis.call('HGETALL', key)
  for i = 1, #raw, 2 do out[raw[i]] = raw[i + 1] end
  return out
end

-- HSET every field of `tbl` (values coerced with sk.str). No-op on an empty table.
function sk.hset(key, tbl)
  local flat = {}
  for k, v in pairs(tbl) do
    flat[#flat + 1] = k
    flat[#flat + 1] = sk.str(v)
  end
  if #flat > 0 then redis.call('HSET', key, unpack(flat)) end
end

-- Append one transition: XADD to `stream` (capped at ~maxlen) and PUBLISH
-- `payload` (a JSON string the caller encoded, so a machine keeps its own wire
-- format byte for byte) on `channel`. `fields` is a flat {k1, v1, k2, v2, ...}
-- list, values coerced. Either target may be nil/'' to skip it. Returns the id.
function sk.emit(stream, maxlen, fields, channel, payload)
  local id
  if stream and stream ~= '' then
    local flat = {}
    for i = 1, #fields do flat[i] = sk.str(fields[i]) end
    id = redis.call('XADD', stream, 'MAXLEN', '~', tostring(maxlen), '*', unpack(flat))
  end
  if channel and channel ~= '' and payload then
    redis.call('PUBLISH', channel, payload)
  end
  return id
end

-- Heartbeat: HSET key ts=<now ms> plus `fields`; PEXPIRE when ttl_ms > 0.
function sk.heartbeat(key, ttl_ms, fields)
  local now = sk.now_ms()
  local tbl = { ts = now }
  for k, v in pairs(fields or {}) do tbl[k] = v end
  sk.hset(key, tbl)
  if ttl_ms and ttl_ms > 0 then redis.call('PEXPIRE', key, ttl_ms) end
  return now
end

-- Epoch ms -> "YYYY-MM-DDTHH:MM:SSZ" (UTC). Redis Lua has no os.date, so this
-- is Hinnant's civil_from_days.
function sk.iso(ms)
  local s = math.floor(ms / 1000)
  local days = math.floor(s / 86400)
  local rem = s - days * 86400
  local z = days + 719468
  local era = math.floor(z / 146097)
  local doe = z - era * 146097
  local yoe = math.floor((doe - math.floor(doe / 1460) + math.floor(doe / 36524)
    - math.floor(doe / 146096)) / 365)
  local y = yoe + era * 400
  local doy = doe - (365 * yoe + math.floor(yoe / 4) - math.floor(yoe / 100))
  local mp = math.floor((5 * doy + 2) / 153)
  local d = doy - math.floor((153 * mp + 2) / 5) + 1
  local m = mp < 10 and mp + 3 or mp - 9
  if m <= 2 then y = y + 1 end
  return string.format('%04d-%02d-%02dT%02d:%02d:%02dZ', y, m, d,
    math.floor(rem / 3600), math.floor((rem % 3600) / 60), rem % 60)
end

-- cjson encodes an empty Lua table as {} (an object). For a list that may be
-- empty, encode through this so a reader always gets [].
function sk.json_list(list)
  if #list == 0 then return '[]' end
  return cjson.encode(list)
end
