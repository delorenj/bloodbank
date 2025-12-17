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

The [TARGET] skill describes some scenarios that dictate how I want to use the Bloobank CLI. The goal of this task is to implement the Bloodbank Event Producer CLI according to those specifications.

## Requirements

[project.scripts]
bb = "event_producers.cli:app"
bloodbank = "event_producers.cli:app"
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
```
