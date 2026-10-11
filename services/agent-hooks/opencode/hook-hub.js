import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

// OpenCode v2 plugin. One native adapter, no behavioral hooks.
//
// V1 returned a hooks object and OpenCode >=2.0 never calls it. The V2 contract
// is `export default { id, setup(ctx) }`: hooks register on the domain that owns
// them, the event stream is a subscription, and the cleanup function returned by
// `setup` is the dispose hook. `Plugin.define` is the identity function, so the
// literal needs no import and no node_modules.
//
// The native names the hub sees are unchanged (session.created, chat.message,
// tool.execute.before/after, session.idle, session.error, session.deleted,
// permission.asked), so hooks.master.json and event_map.generated.json do not move.
//
// Where each native name now comes from:
//   session.created       event "session.created", or the first prompt of a session
//                         this instance never saw created (see ensureSession)
//   chat.message          ctx.session.hook("prompt")
//   tool.execute.before   ctx.tool.hook("execute.before")
//   tool.execute.after    ctx.tool.hook("execute.after"); its `status: "error"`
//                         replaces V1's message.part.updated error-part workaround
//   session.idle          event "session.execution.succeeded" / "interrupted"
//                         (or "session.idle" when the host emits it; one per turn)
//   session.error         event "session.execution.failed"
//   session.deleted       event "session.deleted"
//   permission.asked      event "permission.asked"
//
// Payload parity with v1: `model` on chat.message comes from the session's last
// model request or selection, else ctx.session.get; a first turn on the default
// model has none yet (v1 always had one), and the key is then omitted.
// Shutdown: `opencode run` unloads plugins the moment its last turn ends, so each
// bb-hook child gets its own process group and the cleanup function waits up to
// SHUTDOWN_GRACE_MS for in-flight hooks. Without both, the turn-end hook is lost.
// Injected context is held in memory per user message, not persisted: a restarted
// server does not replay earlier turns' hub context (v1 persisted a synthetic part).
//
// Pane attribution (ZELLIJ_PANE_ID / ZELLIJ_SESSION_NAME): this plugin runs
// inside ONE long-lived server process that serves every session — under
// `opencode serve --service` that process was started in some terminal once,
// and its own environment names a pane that has usually been closed for weeks.
// Publishing the server's pane would attribute every session's events to a
// dead pane (deckard refuses them; nothing lights up). The only place v2
// exposes a session's OWN environment is the shell `create.before` hook, which
// fires per spawned shell with the resolved env but no sessionID. So:
// `tool.execute.before` records the pending shell call (command + cwd, both
// sides of the pair verified), and the next `create.before` with exactly that
// (command, cwd) binds that session's terminal identity for every later
// bb-hook spawn. Zero or multiple waiters for the key bind nothing — a wrong
// pane is worse than none. Sessions that never spawn a shell keep the
// process env (pre-existing behaviour, no regression).

const MAX_CAPTURED = 2048;
const MAX_INJECTED = 256;
const BB_TIMEOUT_MS = 4000;
const BB_SLOW_TIMEOUT_MS = 16000;
// `opencode run` unloads plugins the instant the last turn ends, so the cleanup
// function gives hooks that are still in flight (the turn-completed one above
// all) this long to land before it kills them.
const SHUTDOWN_GRACE_MS = 3000;

const remember = (set, value, cap) => {
  set.add(value);
  if (set.size > cap) set.delete(set.values().next().value);
};

const rememberEntry = (map, key, value, cap) => {
  map.delete(key);
  map.set(key, value);
  if (map.size > cap) map.delete(map.keys().next().value);
};

const errorMessage = (error) => {
  if (!error) return undefined;
  if (typeof error === "string") return error;
  if (typeof error.message === "string") return error.message;
  try { return JSON.stringify(error); } catch { return String(error); }
};

const outputText = (result) => {
  if (!result) return undefined;
  const text = (result.content || [])
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
  if (text) return text;
  if (typeof result.output === "string") return result.output;
  if (result.output === undefined) return undefined;
  try { return JSON.stringify(result.output); } catch { return undefined; }
};

const additionalContext = (raw) => {
  if (!raw.trim()) return "";
  try {
    const value = JSON.parse(raw);
    return value.hookSpecificOutput?.additionalContext || value.additionalContext || "";
  } catch { return raw.trim(); }
};

// OpenCode's harness prompt reserves <system-reminder> for instructions that are
// not user-authored. V1 marked its injected part `synthetic`; V2 has no such
// flag on a message part, so the wrapper is how the model tells hub context from
// the user's own words.
const reminder = (text) => `<system-reminder>\n${text}\n</system-reminder>`;

export default {
  id: "bloodbank.hook-hub",
  setup(ctx) {
    const command = process.env.BB_HOOK_COMMAND || join(homedir(), ".agents/hooks/bb-hook");
    const home = ctx.location.directory;
    const stop = new AbortController();
    const children = new Set();
    const sessions = new Map();     // sessionID -> { context, startup, consumed }
    const owned = new Set();        // sessions with direct evidence they belong to this location
    const directories = new Map();  // sessionID -> working directory
    const turns = new Map();        // sessionID -> messageID of the latest prompt
    const models = new Map();       // sessionID -> { providerID, modelID } from the last model request
    const running = new Set();      // sessionIDs with a turn in flight
    const toolInputs = new Map();
    const completedCalls = new Set();
    const handledPrompts = new Set();
    const injected = new Map();     // user messageID -> context text for that turn
    const chains = new Map();       // sessionID -> tail of that session's event queue
    const pending = new Set();      // emit() promises still waiting on bb-hook
    const sessionEnv = new Map();   // sessionID -> { pane, session } captured from its own shells
    const pendingShellCalls = new Map(); // `${command}\0${cwd}` -> [sessionID, ...] awaiting create.before

    const directoryOf = (sessionID) => directories.get(sessionID) || home;

    const own = (sessionID) => {
      if (!sessionID) return;
      remember(owned, sessionID, MAX_CAPTURED);
    };

    const emit = (native, payload, directory = home) => {
      const call = send(native, payload, directory);
      pending.add(call);
      call.then(() => pending.delete(call));
      return call;
    };

    const send = (native, payload, directory) => new Promise((resolve) => {
      const slow = native === "chat.message" || native === "session.created";
      const args = ["--cli", "opencode", "--native", native];
      if (slow) args.push("--deadline", "15");
      // Terminal identity: the session's own captured env when there is one,
      // else the server's. A session whose own shells carry no ZELLIJ_* at all
      // (headless) publishes pane-less events rather than the server's stale
      // pane — deckard ignores the missing pane and refuses the dead one, and
      // only one of those spams the log forever.
      let env = process.env;
      const captured = payload.session_id ? sessionEnv.get(payload.session_id) : undefined;
      if (captured) {
        env = { ...process.env };
        if (captured.pane) env.ZELLIJ_PANE_ID = captured.pane;
        else delete env.ZELLIJ_PANE_ID;
        if (captured.session) env.ZELLIJ_SESSION_NAME = captured.session;
        else delete env.ZELLIJ_SESSION_NAME;
      }
      let child;
      try {
        // Own process group: `opencode run` tears its server down the moment the
        // last turn ends, and a hook still starting up (the turn-completed one)
        // must outlive that.
        child = spawn(command, args, { cwd: directory, env, stdio: ["pipe", "pipe", "ignore"], detached: true });
        child.unref();
      } catch { resolve(""); return; }
      children.add(child);
      let output = "";
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        children.delete(child);
        resolve(output);
      };
      const timer = setTimeout(() => { child.kill("SIGKILL"); finish(); }, slow ? BB_SLOW_TIMEOUT_MS : BB_TIMEOUT_MS);
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => { if (output.length < 65536) output += chunk; });
      child.on("error", finish);
      child.on("close", finish);
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify({ cwd: directory, hook_event_name: native, ...payload }));
    });

    // A session this instance did not see created (the first session in a
    // location is created before its plugins load) is told apart from a resumed
    // one by whether it already holds messages: "resume" makes the hub skip the
    // Hindsight briefing, which a genuinely new session still needs.
    const startupSource = async (sessionID) => {
      try {
        const messages = await ctx.session.context({ sessionID });
        return Array.isArray(messages) && messages.length === 0 ? "startup" : "resume";
      } catch { return "resume"; }
    };

    // V1 handed chat.message the model. A first turn has no model event or model
    // request behind it yet, so ask the session what it will run.
    const sessionModel = async (sessionID) => {
      try {
        const info = await ctx.session.get({ sessionID });
        if (!info?.model) return undefined;
        const model = { providerID: info.model.providerID, modelID: info.model.id };
        models.set(sessionID, model);
        return model;
      } catch { return undefined; }
    };

    const ensureSession = (sessionID, source) => {
      if (!sessionID) return;
      let session = sessions.get(sessionID);
      if (!session) {
        session = { context: "" };
        sessions.set(sessionID, session);
        session.startup = (async () => {
          const resolved = source || await startupSource(sessionID);
          const raw = await emit("session.created", { session_id: sessionID, source: resolved }, directoryOf(sessionID));
          if (sessions.get(sessionID) === session) session.context = additionalContext(raw);
        })();
      }
      return session;
    };

    const forget = (sessionID) => {
      sessions.delete(sessionID);
      owned.delete(sessionID);
      directories.delete(sessionID);
      turns.delete(sessionID);
      models.delete(sessionID);
      running.delete(sessionID);
      sessionEnv.delete(sessionID);
    };

    const completeTurn = async (sessionID) => {
      if (!running.delete(sessionID)) return;
      await emit("session.idle", { sessionID, session_id: sessionID, turn_id: turns.get(sessionID) }, directoryOf(sessionID));
    };

    const failToolCall = async (sessionID, callID, error) => {
      const key = `${sessionID}:${callID}`;
      const pending = toolInputs.get(key);
      if (!pending) return;
      toolInputs.delete(key);
      remember(completedCalls, key, MAX_CAPTURED);
      await emit("tool.execute.after", { ...pending, is_error: true, error }, directoryOf(sessionID));
    };

    const onEvent = async (event) => {
      const data = event.data || {};
      const sessionID = data.sessionID;
      if (!sessionID) return;
      if (event.type === "session.created") {
        // The stream is the server's, not this location's: take only sessions created here.
        const where = data.location || event.location;
        if (where?.directory && where.directory !== home) return;
        own(sessionID);
        directories.set(sessionID, where?.directory || home);
        // Not awaited: the first prompt waits on the startup itself, and holding the
        // session's event queue here would park a delete behind a slow hub call.
        ensureSession(sessionID, "startup");
        return;
      }
      if (!owned.has(sessionID)) return;
      switch (event.type) {
        case "session.deleted":
          forget(sessionID);
          await emit("session.deleted", { session_id: sessionID, reason: "deleted" }, home);
          break;
        case "session.model.selected":
          // V1 put the model on chat.message; V2's prompt hook no longer carries it.
          if (data.model) models.set(sessionID, { providerID: data.model.providerID, modelID: data.model.id });
          break;
        case "session.execution.started":
          running.add(sessionID);
          break;
        case "session.execution.succeeded":
        case "session.idle":
          await completeTurn(sessionID);
          break;
        case "session.execution.interrupted":
          // A steering prompt replaces the run and a shutdown ends the server;
          // neither leaves the session idle.
          if (data.reason === "superseded" || data.reason === "shutdown") break;
          await completeTurn(sessionID);
          break;
        case "session.execution.failed":
          await emit("session.error", {
            sessionID, session_id: sessionID, turn_id: turns.get(sessionID), error: data.error,
          }, directoryOf(sessionID));
          await completeTurn(sessionID);
          break;
        case "permission.asked":
          await emit("permission.asked", {
            ...data, session_id: sessionID, turn_id: turns.get(sessionID),
            permission: data.action, patterns: data.resources, tool_name: data.action,
          }, directoryOf(sessionID));
          break;
        case "session.tool.failed":
          // execute.after reports a failed tool; this only closes a call whose
          // after hook never ran (a call stopped before it executed).
          await failToolCall(sessionID, data.id, errorMessage(data.error));
          break;
        default:
      }
    };

    // Events of one session stay ordered; different sessions never wait on each other.
    const dispatch = (event) => {
      const sessionID = event?.data?.sessionID || "";
      const previous = chains.get(sessionID) || Promise.resolve();
      const next = previous.then(() => onEvent(event)).catch(() => {});
      chains.set(sessionID, next);
      next.then(() => { if (chains.get(sessionID) === next) chains.delete(sessionID); });
    };

    const pump = async () => {
      let delay = 250;
      while (!stop.signal.aborted) {
        try {
          for await (const event of ctx.event.subscribe({ signal: stop.signal })) {
            delay = 250;
            dispatch(event);
          }
        } catch { /* reconnect below */ }
        if (stop.signal.aborted) return;
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, 10000);
      }
    };

    const register = async () => {
      await ctx.session.hook("prompt", async (event) => {
        try {
          const { sessionID, messageID } = event;
          own(sessionID);
          // Prompt hooks are retry-safe, not exactly-once.
          if (handledPrompts.has(messageID)) return;
          remember(handledPrompts, messageID, MAX_CAPTURED);
          const session = ensureSession(sessionID);
          const firstMessage = session && !session.consumed;
          if (firstMessage) session.consumed = true;
          await session?.startup;
          if (messageID && sessions.get(sessionID) === session) turns.set(sessionID, messageID);
          running.add(sessionID);
          const model = models.get(sessionID) || await sessionModel(sessionID);
          const raw = await emit("chat.message", {
            session_id: sessionID, turn_id: messageID, model, prompt: event.prompt?.text ?? "",
          }, directoryOf(sessionID));
          const text = [
            firstMessage && sessions.get(sessionID) === session ? session.context : "",
            additionalContext(raw),
          ].filter(Boolean).join("\n\n");
          if (firstMessage) session.context = "";
          if (text) rememberEntry(injected, messageID, text, MAX_INJECTED);
        } catch { /* a hub failure must never reject the user's prompt */ }
      });

      // Model-visible context for the turn. The prompt hook cannot attach a hidden
      // part (it would rewrite the user's own text), so the hub's context rides
      // along on the matching user message of every model request in that turn.
      // Nothing is persisted, and each request sees identical history.
      await ctx.session.hook("context", (event) => {
        try {
          if (event.model) {
            models.set(event.sessionID, { providerID: event.model.providerID, modelID: event.model.id });
          }
          if (!injected.size) return;
          for (const message of event.messages) {
            if (message.role !== "user") continue;
            const text = injected.get(message.id);
            if (!text) continue;
            const part = { type: "text", text: reminder(text) };
            if (Array.isArray(message.content)) message.content.push(part);
            else message.content = [{ type: "text", text: String(message.content ?? "") }, part];
          }
        } catch { /* never break a model request over injected context */ }
      });

      await ctx.tool.hook("execute.before", async (event) => {
        try {
          const sessionID = event.sessionID;
          own(sessionID);
          const payload = {
            session_id: sessionID, turn_id: turns.get(sessionID),
            tool_name: event.tool, tool_call_id: event.id, tool_input: event.input,
          };
          rememberEntry(toolInputs, `${sessionID}:${event.id}`, payload, MAX_CAPTURED);
          // A shell tool call is about to spawn: its `create.before` will carry
          // this session's own environment (see the pane-attribution note up
          // top). Keyed by the exact (command, cwd) pair, which both sides
          // report, so the bind is exact or absent — never guessed.
          if (event.input && typeof event.input.command === "string") {
            const cwd = (typeof event.input.workdir === "string" && event.input.workdir)
              || directoryOf(sessionID);
            const key = `${event.input.command}\u0000${cwd}`;
            const waiting = pendingShellCalls.get(key) || [];
            waiting.push(sessionID);
            rememberEntry(pendingShellCalls, key, waiting, MAX_CAPTURED);
          }
          await emit("tool.execute.before", payload, directoryOf(sessionID));
        } catch { /* never block a tool over telemetry */ }
      });

      await ctx.tool.hook("execute.after", async (event) => {
        try {
          const sessionID = event.sessionID;
          own(sessionID);
          const key = `${sessionID}:${event.id}`;
          if (completedCalls.has(key)) return;
          remember(completedCalls, key, MAX_CAPTURED);
          const previous = toolInputs.get(key) || { turn_id: turns.get(sessionID), tool_input: event.input };
          toolInputs.delete(key);
          const failed = event.status === "error";
          await emit("tool.execute.after", {
            ...previous, session_id: sessionID, tool_name: event.tool, tool_call_id: event.id,
            tool_output: failed ? undefined : outputText(event.result),
            is_error: failed,
            ...(failed ? { error: errorMessage(event.error) } : {}),
          }, directoryOf(sessionID));
        } catch { /* never block a tool over telemetry */ }
      });

      // The one v2 seam that sees a session's own environment: fired per
      // spawned shell, with the resolved env (the session's, else the server's)
      // but no sessionID. Bound to the pending shell tool call by the exact
      // (command, cwd) pair recorded in `execute.before`. Shells spawned
      // outside the shell tool (or with an ambiguous key) bind nothing.
      // Guarded: a host build without the shell domain keeps the legacy
      // process-env attribution rather than failing the whole plugin.
      try {
        await ctx.shell.hook("create.before", (event) => {
          try {
            const key = `${event.command}\u0000${event.cwd}`;
            const waiting = pendingShellCalls.get(key);
            pendingShellCalls.delete(key);
            if (!waiting || waiting.length !== 1) return;
            const env = event.env || {};
            rememberEntry(sessionEnv, waiting[0], {
              pane: typeof env.ZELLIJ_PANE_ID === "string" ? env.ZELLIJ_PANE_ID : null,
              session: typeof env.ZELLIJ_SESSION_NAME === "string" ? env.ZELLIJ_SESSION_NAME : null,
            }, MAX_CAPTURED);
          } catch { /* never break a shell over attribution capture */ }
        });
      } catch { /* no shell domain on this host build */ }
    };

    // The subscription is open before the hooks register so a session created in
    // between is not missed; register() returns a promise, so setup is async.
    void pump();
    return register().then(() => async () => {
      stop.abort();
      const drained = Promise.allSettled([...chains.values(), ...pending]);
      let grace;
      await Promise.race([drained, new Promise((resolve) => { grace = setTimeout(resolve, SHUTDOWN_GRACE_MS); })]);
      clearTimeout(grace);
      for (const child of children) child.kill("SIGKILL");
      children.clear();
      sessions.clear();
      owned.clear();
      injected.clear();
      toolInputs.clear();
      sessionEnv.clear();
      pendingShellCalls.clear();
    });
  },
};
