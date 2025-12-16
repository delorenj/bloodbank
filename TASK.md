# TASK

## References

### Skills

[LEGACY] `./claude_skills/bloodbank_event_publisher/SKILL.md`
The original skill can be found here.

[LEGACY] `./claude_updates/SKILL.md`
This skill was created after changing correlation IDs to arrays and adding error events.

[TARGET] `/home/delorenj/.claude/skills/bloodbank-n8n-event-driven-workflows/SKILL.md`
This is the ACTUAL skill used in Claude's environment. I am taking it upon myself to write it from scratch to ensure it works exactly as intended.

### Code

`./event_producers/cli.py`
This is the code for the Bloodbank Event Producer CLI.

`./event_producers/events.py`
This is where all the event definitions are currently stored.

## Goal

The [TARGET] skill describes some scenarios that dictate how I want to use the Bloodbank CLI. The goal of this task is to implement the Bloodbank Event Producer CLI according to those specifications.

## Requirements

- [ ] I can run `bloodbank list-events` to see a list of all available events.
- [ ] I can run `bb list-commands` to see a list of all available commands (subset of mutable events).
- [ ] I can run `bloodbank publish someEventName [--mock|-m]` to publish an event using the mock data defined in the `./someEventName/mock.json` file.
- [ ] I can run `bloodbank show someEventName` to see the full event definition for `someEventName`.
- [ ] I can run `bloodbank help` to see a help menu broken down by 'Events', 'Commands', 'Publishing Events', and 'Adding Events', 'Extending Types' (content can be sparse, just need the structure).
- [ ] The CLI should provide helpful error messages when an invalid event name or command is provided
- [ ] All events should derive from `BaseEvent`
- [ ] All commands should derive from `Command` (which itself derives from BaseEvent)
- [ ] Events and commands should be organized by domain (e.g. fireflies, github, llm, task, etc)
- [ ] Events should be organized into folders called Event Modules. Each EventModule should be self-contained and include its own event definitions, mock data, type definitions, factory classes, and builder classes.

Example folder structure:

```
./events/
|-- fireflies/
|   |-- AudioUploaded/
|       |-- FirefliesAudioUploadedEvent.py
|       |-- FirefliesAudioUploadedMock.json
|       |-- FirefliesAudioUploadedType.py
<!--toc:start-->

- [TASK](#task)
<!--toc:end-->

Please add this new event typedef to @event_producers/events.py

```json
{
  "event_id": "trinote|423|1761143370158",
  "event_type": "github.pr.created",
  "timestamp": "2025-10-22T14:29:30.158Z",
  "version": "1.0.0",
  "source": {
    "component": "n8n",
    "host_id": "big-chungus",
    "workflow_id": null,
    "workflow_name": null,
    "execution_id": null
  },
  "correlation_id": null,
  "correlation_ids": [],
  "agent_context": {
    "agent": "Tonny"
  },
  "payload": {
    "cache_key": "trinote|423",
    "cache_type": "redis"
  }
}
```
