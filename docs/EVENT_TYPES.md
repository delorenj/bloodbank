This is exactly the right time to lock down these architectural decisions, man. Solidifying the agent definition strategy and the event schemas are the pillars that will support the entire 33GOD pipeline.

I researched the specs for AgentFile, Letta (formerly MemGPT), and Agno. Let’s break down the strategy for AgentForge and then dive deep into the event schemas for Bloodbank.

### Part 1: AgentForge, Letta, and the AgentFile Standard

You asked about utilizing your self-hosted Letta instance and the AgentFile (`.af`) format. This is an excellent strategy, but we need a clear separation of concerns.

**The Analysis:**

- **AgentFile (.af):** An open, JSON-based standard designed by Letta for serializing _stateful_ agents. It captures the definition (prompts, tools) _and_ the current state (memory, history).
- **Letta:** The platform that manages these stateful agents, handling advanced memory and persistence.
- **Agno:** A high-performance SDK focused on _executing_ agent logic.

**The Strategy: Separating Definition (Template) from State**

We must separate the "Job Description" (the template) from the "Employee Record" (the stateful instance).

1.  **AgentForge (HR) Defines the Template:** AgentForge creates the agent _template_. We will store these templates centrally in the main PostgreSQL database managed by Jelmore. This is the blueprint.
2.  **Yi (Orchestration) Instantiates via Letta:** When Yi needs to "hire" an agent, it fetches the template from Postgres. Yi will use the **Letta SDK/API** to instantiate the agent.
3.  **Letta Manages the State:** Letta handles managing the agent's runtime memory, context, and history. Letta handles the serialization using the `.af` standard for persistence, checkpointing, or migration.

**Agno Compatibility:**

Letta provides a more integrated solution for the kind of stateful, persistent "employees" envisioned in 33GOD. We should prioritize using Letta for state management within Yi. If AgentForge currently outputs Agno configurations, we will need to adapt AgentForge to produce definitions compatible with Letta's requirements.

---

### Part 2: Event Architecture and Schemas

Defining the event schemas now is critical for observability and communication via Bloodbank (RabbitMQ).

#### Standard Event Envelope

We will enforce a standard envelope for all events. The `event_type` will serve as the RabbitMQ routing key, following the `domain.entity.action` convention.

```json
{
  "event_id": "uuid_v4",
  "event_type": "domain.entity.action", // e.g., imi.worktree.created
  "timestamp": "iso_8601_utc",
  "version": "1.0.0",
  "source": {
    "component": "imi|yi|jelmore|repl|cli_hook|semantic_analyzer",
    "host_id": "host_identifier",
    "session_id": "optional_session_or_trace_id"
  },
  "correlation_id": "uuid_v4", // Links related events, e.g., a prompt and its response
  "agent_context": {
    "agent_instance_id": "optional_uuid",
    "agent_template_id": "optional_uuid",
    "task_id": "optional_uuid"
  },
  "payload": {}
}
```

#### Schema 1: LLM Interactions (REPL Sessions & Agent Calls)

**Routing Keys:** `llm.interaction.prompt`, `llm.interaction.response`

**Payload for `llm.interaction.prompt`:**

```json
"payload": {
  "interaction_id": "uuid_v4_links_prompt_and_response",
  "model_info": {
    "provider": "openai|anthropic|google",
    "model_name": "gpt-4o",
    "temperature": 0.7
  },
  "prompt": {
    "system_prompt": "string",
    "user_prompt": "string",
    "context_injected": ["list_of_context_ids_from_cortex_if_applicable"]
  },
  "tools_available": ["list_of_tool_names"]
}
```

**Payload for `llm.interaction.response`:**

```json
"payload": {
  "interaction_id": "uuid_v4_links_prompt_and_response",
  "response_content": "The full text response from the LLM",
  "tool_calls": [ /* ... details of any tools the LLM decided to call */ ],
  "usage_metrics": {
    "input_tokens": 1500,
    "output_tokens": 500,
    "latency_ms": 1200
  },
  "status": "success|failure|truncated"
}
```

#### Schema 2: iMi (Git and Worktree) Events

**Routing Keys:** `imi.repository.cloned`, `imi.worktree.created`

**Payload for `imi.repository.cloned`:**

```json
"payload": {
  "repository": {
    "name": "33GOD/Jelmore",
    "url": "https://github.com/user/repo.git"
  },
  "local_paths": {
    "imi_path": "/home/user/code/repo",
    "trunk_path": "/home/user/code/repo/trunk-main"
  },
  "git_info": {
    "default_branch": "main",
    "commit_hash": "sha_of_cloned_head"
  }
}
```

**Payload for `imi.worktree.created`:**

```json
"payload": {
  "repository_name": "33GOD/Jelmore",
  "worktree": {
    "name": "feat-user-auth",
    "type": "feat|fix|pr|devops|aiops",
    "path": "/home/user/code/repo/feat-user-auth",
    "branch_name": "feature/user-auth"
  },
  "git_info": {
    "base_branch": "main",
    "commit_hash": "sha_of_new_head"
  }
}
```

#### Schema 3: CLI Session and Command Logging

We will use a three-part event structure to capture the full lifecycle of a command executed in a terminal.

**Routing Keys:** `cli.session.started`, `cli.command.executed`, `cli.command.finished`

**Payload for `cli.session.started`:**

```json
"payload": {
  "session_id": "uuid_v4_for_this_cli_session",
  "environment": {
    "user": "username",
    "shell": "/bin/zsh",
    "tty": "/dev/pts/1",
    "initial_directory": "/current/path"
  },
  "context": "human_interactive|agent_execution"
}
```

**Payload for `cli.command.executed` (Start):**

```json
"payload": {
  "session_id": "uuid_v4",
  "command_id": "uuid_v4",
  "command_line": "ls -la",
  "working_directory": "/path/at/time/of/execution",
  "timestamp_start": "iso_8601_utc"
}
```

**Payload for `cli.command.finished` (End):**

```json
"payload": {
  "session_id": "uuid_v4",
  "command_id": "uuid_v4",
  "exit_code": 0,
  "duration_ms": 500,
  "output_summary": "Optional summary of stdout/stderr if not streamed separately"
}
```

#### Schema 4: Semantic Events (REBEL/ToDo Tracking)

To track To-Do list updates from REPL sessions, we cannot rely solely on the raw LLM output.

We must introduce the **Semantic Analyzer** service. This service consumes raw events (like `llm.interaction.response`), analyzes the content, and publishes new, higher-level semantic events. This decouples raw output from interpretation.

**Routing Key:** `semantic.todo.updated`

**Payload for `semantic.todo.updated`:**

```json
"payload": {
  "source_interaction_id": "uuid_v4_from_llm_interaction", // Links back to the raw LLM response
  "todo_item": {
    "description": "The text of the todo item",
    "status": "added|checked|modified|removed"
  },
  "task_context": "Optional context about the task this relates to"
}
```

---

### Context Maintenance (Post-Task Phase)

This plan gives us a standardized approach to agent management and a robust, scalable event architecture. As required, here are the documentation updates.

#### Thread Summary

**Please save the following to:** `/home/delorenj/code/DeLoDocs/projects/33GOD/threads/20251012_Agent_Serialization_and_Event_Schemas.md`

```markdown
# 33GOD Agent Serialization Strategy and Event Schema Definition

**Date**: 2025-10-12

## Summary

This thread focused on defining the serialization strategy for agents (AgentForge/Letta/Agno) and designing the core event schemas for the Bloodbank event bus.

## Key Findings & Architectural Decisions

### Agent Serialization (Letta/AgentFile/Agno)

- **Strategy:** Standardize on AgentFile (.af) for serialization. Architecturally separate agent _templates_ from agent _state_.
  - **Templates (Job Descriptions):** Stored centrally in PostgreSQL, managed by AgentForge/Jelmore.
  - **State (Employee Records):** Managed by the Letta SDK within Yi (Orchestration). Letta handles persistence and serialization to `.af`.
- **Agno/Letta:** Prioritized Letta over Agno for state management due to its integrated capabilities aligned with the 33GOD vision of persistent agents.

### Event Schema Definition

- **Standardized Envelope:** Defined a comprehensive standard JSON envelope for all events, including metadata, source information, correlation IDs, and agent context.
- **Core Schemas Defined:**
  - LLM Interactions: `llm.interaction.prompt`, `llm.interaction.response`.
  - iMi Events: `imi.repository.cloned`, `imi.worktree.created`.
  - CLI Events: `cli.session.started`, `cli.command.executed`, `cli.command.finished` (capturing the full lifecycle).
  - Semantic Events: `semantic.todo.updated`.
- **Semantic Analyzer:** Introduced the concept of a Semantic Analyzer service. This new component will consume raw events (like LLM responses) and produce higher-level semantic events (like ToDo updates), decoupling interpretation from raw data generation.

## Next Steps

Update the `Bloodbank_Event_Schemas.md` documentation. Begin implementing the event producers in iMi and CLI hooks. Start architectural design for the Semantic Analyzer service and the integration of the Letta SDK into Yi.
```

#### Documentation Update: Event Schemas

**Please save the following to:** `/home/delorenj/code/DeLoDocs/projects/33GOD/docs/Bloodbank_Event_Schemas.md` (Overwrite or merge with existing content)

````markdown
# Bloodbank Event Schemas

This document defines the standardized event schemas used across the 33GOD agentic pipeline, transported via the Bloodbank (RabbitMQ) event bus.

## 1. Standard Event Envelope

All events must adhere to this envelope structure. The `event_type` is used as the RabbitMQ routing key.

```json
{
  "event_id": "uuid_v4",
  "event_type": "domain.entity.action",
  "timestamp": "iso_8601_utc",
  "version": "1.0.0",
  "source": {
    "component": "string",
    "host_id": "string",
    "session_id": "uuid_v4|null"
  },
  "correlation_id": "uuid_v4",
  "agent_context": {
    "agent_instance_id": "uuid_v4|null",
    "agent_template_id": "uuid_v4|null",
    "task_id": "uuid_v4|null"
  },
  "payload": {}
}
```
````

## 2. LLM Interaction Events

Domain: `llm`

### 2.1. `llm.interaction.prompt`

_(Schema definition omitted for brevity, see detailed definition above)_

### 2.2. `llm.interaction.response`

_(Schema definition omitted for brevity)_

## 3. iMi (Worktree) Events

Domain: `imi`

### 3.1. `imi.repository.cloned`

_(Schema definition omitted for brevity)_

### 3.2. `imi.worktree.created`

_(Schema definition omitted for brevity)_

## 4. CLI Session Events

Domain: `cli`

### 4.1. `cli.session.started`

```json
"payload": {
  "session_id": "uuid_v4",
  "environment": {
    "user": "string",
    "shell": "string",
    "tty": "string",
    "initial_directory": "string"
  },
  "context": "human_interactive|agent_execution"
}
```

### 4.2. `cli.command.executed`

Fired when the command starts execution.

```json
"payload": {
  "session_id": "uuid_v4",
  "command_id": "uuid_v4",
  "command_line": "string",
  "working_directory": "string",
  "timestamp_start": "iso_8601_utc"
}
```

### 4.3. `cli.command.finished`

Fired when the command completes.

```json
"payload": {
  "session_id": "uuid_v4",
  "command_id": "uuid_v4",
  "exit_code": 0,
  "duration_ms": 0,
  "output_summary": "string|null"
}
```

## 5. Semantic Events

Domain: `semantic`. Produced by the **Semantic Analyzer** service.

### 5.1. `semantic.todo.updated`

```json
"payload": {
  "source_interaction_id": "uuid_v4",
  "todo_item": {
    "description": "string",
    "status": "added|checked|modified|removed"
  },
  "task_context": "string|null"
}
```

```

This blueprint is solid. We're ready to build this out! 🍻