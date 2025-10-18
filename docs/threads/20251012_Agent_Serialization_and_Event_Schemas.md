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