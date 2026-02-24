"""FastStream-based Bloodbank agent consumer template.

Queue convention:
  agent.{name}.inbox (durable)
Binding:
  exchange bloodbank.events.v1 (TOPIC), routing key agent.{name}.#
Retry/DLQ policy:
  - success => ACK
  - failure => reject -> dead-letter to retry queue chain
  - 3 retries with exponential backoff (5s, 30s, 120s)
  - final dead-letter queue: agent.{name}.dlq
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from faststream import FastStream
from faststream.middlewares.acknowledgement.config import AckPolicy
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue

EnvelopeHandler = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[None]]


@dataclass
class ConsumerConfig:
    agent_name: str
    rabbitmq_url: str = os.environ.get("RABBITMQ_URL", "amqp://delorenj:MISSING_PASSWORD@rabbitmq:5672/")
    exchange_name: str = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
    retry_ttls_ms: tuple[int, int, int] = (5_000, 30_000, 120_000)

    @property
    def inbox_queue(self) -> str:
        return f"agent.{self.agent_name}.inbox"

    @property
    def dlq_queue(self) -> str:
        return f"agent.{self.agent_name}.dlq"

    def retry_queue(self, idx: int) -> str:
        return f"agent.{self.agent_name}.retry.{idx}"


class AgentConsumer:
    def __init__(self, config: ConsumerConfig, handler: EnvelopeHandler):
        self.config = config
        self.handler = handler

        self.broker = RabbitBroker(self.config.rabbitmq_url)
        self.app = FastStream(self.broker)
        self.exchange = RabbitExchange(
            name=self.config.exchange_name,
            type=ExchangeType.TOPIC,
            durable=True,
        )

        self._register_subscriber()

    def _register_subscriber(self) -> None:
        c = self.config

        # Main inbox queue. On reject/error, dead-letter to retry.1
        inbox = RabbitQueue(
            c.inbox_queue,
            durable=True,
            routing_key=f"agent.{c.agent_name}.#",
            arguments={
                "x-dead-letter-exchange": c.exchange_name,
                "x-dead-letter-routing-key": c.retry_queue(1),
            },
        )

        # Retry queues: ttl -> dead-letter forward (exp backoff chain)
        retry1 = RabbitQueue(
            c.retry_queue(1),
            durable=True,
            routing_key=c.retry_queue(1),
            arguments={
                "x-message-ttl": c.retry_ttls_ms[0],
                "x-dead-letter-exchange": c.exchange_name,
                "x-dead-letter-routing-key": c.retry_queue(2),
            },
        )
        retry2 = RabbitQueue(
            c.retry_queue(2),
            durable=True,
            routing_key=c.retry_queue(2),
            arguments={
                "x-message-ttl": c.retry_ttls_ms[1],
                "x-dead-letter-exchange": c.exchange_name,
                "x-dead-letter-routing-key": c.retry_queue(3),
            },
        )
        retry3 = RabbitQueue(
            c.retry_queue(3),
            durable=True,
            routing_key=c.retry_queue(3),
            arguments={
                "x-message-ttl": c.retry_ttls_ms[2],
                "x-dead-letter-exchange": c.exchange_name,
                "x-dead-letter-routing-key": c.dlq_queue,
            },
        )

        # Final DLQ (bind explicitly)
        dlq = RabbitQueue(
            c.dlq_queue,
            durable=True,
            routing_key=c.dlq_queue,
        )

        # Ensure queues are declared by registering no-op subscribers for retry/DLQ
        # (no handlers needed for retry queues; messages TTL/dead-letter automatically).
        @self.broker.subscriber(retry1, exchange=self.exchange, ack_policy=AckPolicy.ACK)
        async def _declare_retry1(_: dict[str, Any]) -> None:
            return

        @self.broker.subscriber(retry2, exchange=self.exchange, ack_policy=AckPolicy.ACK)
        async def _declare_retry2(_: dict[str, Any]) -> None:
            return

        @self.broker.subscriber(retry3, exchange=self.exchange, ack_policy=AckPolicy.ACK)
        async def _declare_retry3(_: dict[str, Any]) -> None:
            return

        @self.broker.subscriber(dlq, exchange=self.exchange, ack_policy=AckPolicy.ACK)
        async def _observe_dlq(message: dict[str, Any]) -> None:
            # Optional hook: write alert/metrics here.
            print(f"[DLQ:{c.agent_name}] message dead-lettered: {json.dumps(message)[:500]}")

        # Main handler: ACK on success, REJECT on exception (sends to retry chain)
        @self.broker.subscriber(
            inbox,
            exchange=self.exchange,
            ack_policy=AckPolicy.REJECT_ON_ERROR,
        )
        async def _consume(envelope: dict[str, Any], msg: Any) -> None:
            routing_key = getattr(getattr(msg, "raw_message", None), "routing_key", "")
            payload = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
            await self.handler(routing_key, payload, envelope)

    async def run(self) -> None:
        await self.app.run()


# --- Minimal drop-in example ---

async def example_handler(routing_key: str, payload: dict[str, Any], envelope: dict[str, Any]) -> None:
    print(f"[consumer] routing_key={routing_key}")
    print(f"[consumer] event_type={envelope.get('event_type')}")
    print(f"[consumer] payload={json.dumps(payload)}")


async def _main() -> None:
    agent_name = os.environ.get("AGENT_NAME", "grolf")
    config = ConsumerConfig(agent_name=agent_name)
    consumer = AgentConsumer(config, example_handler)
    await consumer.run()


if __name__ == "__main__":
    asyncio.run(_main())
