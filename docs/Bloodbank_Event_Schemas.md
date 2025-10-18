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
