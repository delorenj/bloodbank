Goal: Create a "Zellij-native" Python TUI to manage the Bloodbank event ecosystem, enforcing a strict migration to
the modern registry-based architecture.

I. Architecture & Migration (Pre-requisites)

1.  Legacy Removal:
    - Task: Delete the events/ directory entirely.
    - Migration: Ensure any unique event definitions in events/ (like FirefliesTranscriptReadyEvent) are fully
      ported to event_producers/events/domains/ as \*Payload classes before deletion.
    - Enforcement: The TUI will only recognize the modern EventRegistry.

2.  Nested Domain Support:
    - Refactor: Update registry.py to support recursive discovery.
      - Current: Scans flat domains/\*.py.
      - New: Scans domains/\*_/_.py.
    - Naming Convention: Map directory structure to routing keys.
      - File: domains/agent/thread.py
      - Routing Key prefix: agent.thread.\*
    - Migration: Move agent_thread.py -> agent/thread.py (and others) as the first test case.

II. Core TUI Functionality

3.  Discovery & Navigation
    - Source of Truth: Exclusively use EventRegistry.auto_discover_domains().
    - Tree View: visualized based on the new nested structure (e.g., agent -> thread -> prompt).

4.  Schema & Payload Management
    - Viewer: Read-only view of Pydantic schemas (JSON representation).
    - Mock Data:
      - Load/Edit \*Mock.json files residing next to the domain definition files.
      - Validation: Button to Validate Payload against the Pydantic model in real-time.

5.  Event Actions
    - Publish:
      - Construct an EventEnvelope with the selected payload.
      - Auto-populate metadata (source, timestamp, event_id).
      - Send via HTTP (to the running http.py server).

III. The "Zellij-First" Approach

Instead of a compiled WASM/Rust plugin (which would struggle to read your Python Pydantic models dynamically), I
recommend a Zellij-First Python TUI.

- Implementation: A Python textual app designed specifically to run inside a Zellij pane.
- Integration:
  - Create a specialized Zellij layout file (layouts/bloodbank.kdl).
  - This layout launches the TUI automatically in a locked pane, giving it the look and feel of a native plugin.
- Benefits:
  - Native Performance: Runs directly in your Python environment, instantly importing your actual code.
  - Zero-Overhead: No need to serialize schemas to JSON just to pass them to a WASM plugin.
  - Maintainability: Written in the same language as your events (Python).

Revised Roadmap

1.  Migration & Cleanup:
    - Port missing legacy events -> event_producers/events/domains/.
    - Refactor registry.py for recursive discovery.
    - Delete events/.
    - Restructure domains/ (e.g., create agent/thread.py).

2.  TUI Development (Textual):
    - Build the EventBrowser widget (Tree View).
    - Build the SchemaViewer and PayloadEditor widgets.
    - Implement the Publish logic.

3.  Zellij Integration:
    - Create the bloodbank.kdl layout.
    - Add a mise task to launch the environment: mise run dashboard.
