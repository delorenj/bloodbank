# Gemini Code Assistant Context

This document provides context for the "Bloodbank" project, an event-driven system for tracking and routing events within a software development ecosystem.

## Project Overview

Bloodbank is a Python-based event bus that uses RabbitMQ for message queuing. It provides a central exchange (`bloodbank.events.v1`) where various "producer" applications can publish events. These events are then routed to "consumer" applications that subscribe to specific event types.

The primary goal of Bloodbank is to create a decoupled, scalable, and observable architecture for automating and tracking development-related activities.

### Core Technologies

*   **Python 3.11+**
*   **FastAPI:** For the web server (`http.py`) that ingests events from webhooks.
*   **RabbitMQ:** The message broker.
*   **aio-pika:** Asynchronous Python client for RabbitMQ.
*   **Pydantic:** For data validation and settings management.
*   **Typer:** For the command-line interface (`cli.py`).
*   **Kubernetes:** The target deployment environment (see `kubernetes/deploy.yaml`).
*   **n8n:** For workflow automation (see `n8n/` directory).

### Architecture

The system is composed of the following key components:

*   **Producers:**
    *   `http.py`: A FastAPI server that exposes endpoints to receive events from external sources (e.g., webhooks from services like Fireflies).
    *   `mcp_server.py`: A "Master Control Program" server that provides tools for internal services to publish events.
    *   `cli.py`: A command-line interface for interacting with the system.
*   **RabbitMQ:**
    *   A central `topic` exchange named `bloodbank.events.v1` is used for routing events.
    *   Events are published with a routing key (e.g., `llm.prompt`, `artifact.created`).
*   **Consumers:**
    *   Various applications can consume events by creating their own queues and binding them to the `bloodbank.events.v1` exchange with specific routing key patterns.
    *   Examples include Trello sync, artifact archivers, and analytics services.

### Event Structure

Events are defined in `events.py` using Pydantic models. All events are wrapped in an `EventEnvelope` which contains metadata such as a unique ID, timestamp, source, and correlation ID.

The main event types are:

*   `LLMPrompt`: Represents a prompt sent to a large language model.
*   `LLMResponse`: Represents a response from a large language model.
*   `Artifact`: Represents a created or updated artifact, such as a file, transcript, or image.

## Building and Running

### Dependencies

Install the required Python packages using pip:

```bash
pip install -r requirements.txt
```

*(Note: A `requirements.txt` file is not present, but one could be generated from `pyproject.toml`)*

### Running the Web Server

To run the FastAPI web server locally:

```bash
uvicorn bloodbank.http:app --reload
```

The server will be available at `http://localhost:8682`.

### Running the MCP Server

The MCP server can be run as a standalone process:

```bash
python -m bloodbank.mcp_server
```

### Command-Line Interface

The project includes a CLI for interacting with the system. Use the `--help` flag to see available commands:

```bash
python -m bloodbank.cli --help
```

## Development Conventions

*   **Configuration:** Application settings are managed using Pydantic and environment variables (see `config.py`).
*   **Asynchronous Code:** The project uses `asyncio` and `aio-pika` for non-blocking communication with RabbitMQ.
*   **Testing:** (TODO: Add information about testing practices once test files are available).
*   **Linting and Formatting:** (TODO: Add information about linting and formatting tools if they are used).
