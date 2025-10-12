# Gemini Code Assistant Context

This repository contains two main components:

1.  **Bloodbank Event Bus:** A generic, RabbitMQ-based event bus system.
2.  **Event Producers:** A collection of personal tools and services that produce events for the Bloodbank bus.

## Bloodbank Event Bus

This is the core of the repository. It provides the infrastructure for a generic, event-driven architecture using RabbitMQ.

### Core Technologies

*   **Python 3.11+**
*   **RabbitMQ:** The message broker.
*   **aio-pika:** Asynchronous Python client for RabbitMQ.
*   **Pydantic:** For settings management.
*   **Kubernetes:** The target deployment environment (see `kubernetes/deploy.yaml`).

### Key Files

*   `rabbit.py`: Contains the `Publisher` class for connecting to and publishing messages to RabbitMQ.
*   `config.py`: Manages application settings using Pydantic and environment variables.
*   `pyproject.toml`: Defines project dependencies.
*   `kubernetes/deploy.yaml`: Kubernetes deployment configuration.
*   `TASK.md`: A detailed guide to understanding and using the RabbitMQ event bus.

## Event Producers

This directory contains a collection of personal tools and services that are specific to the user's projects. These tools produce events and publish them to the Bloodbank event bus.

### Core Technologies

*   **FastAPI:** For the web server (`event_producers/http.py`) that ingests events from webhooks.
*   **Typer:** For the command-line interface (`event_producers/cli.py`).
*   **n8n:** For workflow automation (see `event_producers/n8n/` directory).

### Key Files

*   `event_producers/cli.py`: A command-line interface for publishing events.
*   `event_producers/events.py`: Defines the Pydantic models for the events used in the user's personal projects (`LLMPrompt`, `LLMResponse`, `Artifact`).
*   `event_producers/http.py`: A FastAPI server that exposes endpoints for receiving events from external sources (e.g., webhooks).
*   `event_producers/mcp_server.py`: A "Master Control Program" server that provides tools for internal services to publish events.
*   `event_producers/watch.py`: A script that watches a directory for file changes and publishes `artifact.updated` events.
*   `event_producers/n8n/`: Contains n8n workflows for automation.
*   `event_producers/scripts/`: Contains utility scripts, such as a sample `artifact_consumer.py`.

## Building and Running

### Dependencies

Install the required Python packages using pip:

```bash
pip install -e .
```

*(Note: This will install the project in editable mode, which is recommended for development.)*

### Running the Web Server

To run the FastAPI web server for the event producers:

```bash
uvicorn event_producers.http:app --reload
```

The server will be available at `http://localhost:8682`.

### Running the MCP Server

The MCP server can be run as a standalone process:

```bash
python -m event_producers.mcp_server
```

### Command-Line Interface

The project includes a CLI for interacting with the event producers. Use the `--help` flag to see available commands:

```bash
python -m event_producers.cli --help
```