"""
Comprehensive integration tests for bloodbank event publisher v2.0.

Test coverage:
1. CorrelationTracker initialization and connection
2. Deterministic event ID generation (same inputs = same UUID)
3. Adding correlations (single parent, multiple parents)
4. Querying correlation chains (ancestors, descendants)
5. Graceful degradation when Redis unavailable
6. Publisher integration with correlation tracking enabled/disabled
7. Debug endpoints returning correct data

Uses fakeredis for isolated testing without requiring actual Redis/RabbitMQ.
"""

import pytest
import asyncio
from uuid import UUID, uuid4
from datetime import datetime, timezone
from typing import Dict, Any, List
from unittest.mock import AsyncMock, MagicMock, patch
import fakeredis.aioredis
import orjson

# Import modules under test
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from correlation_tracker import CorrelationTracker, link_events, generate_idempotent_id
from rabbit import Publisher
from event_producers.events import (
    EventEnvelope,
    Source,
    FirefliesTranscriptReadyPayload,
    Sentence,
    AIFilters,
    FirefliesUser,
    MeetingInfo,
)


# ============================================================================
# Test Fixtures and Factories
# ============================================================================


class EventFactory:
    """Factory for creating test events with sensible defaults."""

    @staticmethod
    def create_source(
        component: str = "test-component",
        host_id: str = "test-host-123",
        session_id: str = "test-session-456",
    ) -> Source:
        """Create a test Source object."""
        return Source(
            component=component,
            host_id=host_id,
            session_id=session_id,
        )

    @staticmethod
    def create_fireflies_payload(
        meeting_id: str = "meeting-123",
        title: str = "Test Meeting",
    ) -> FirefliesTranscriptReadyPayload:
        """Create a test Fireflies transcript payload."""
        return FirefliesTranscriptReadyPayload(
            id=meeting_id,
            sentences=[
                Sentence(
                    index=0,
                    speaker_name="John Doe",
                    speaker_id=1,
                    raw_text="Hello world",
                    start_time=0.0,
                    end_time=2.5,
                    ai_filters=AIFilters(),
                    text="Hello world",
                )
            ],
            title=title,
            host_email="host@example.com",
            organizer_email="organizer@example.com",
            user=FirefliesUser(
                user_id="user-123",
                email="user@example.com",
                integrations=[],
                user_groups=[],
                name="Test User",
                num_transcripts=1,
                recent_transcript="",
                recent_meeting="",
                minutes_consumed=0.0,
                is_admin=False,
            ),
            fireflies_users=[],
            privacy="private",
            participants=[],
            date=1234567890,
            duration=120.0,
            meeting_info=MeetingInfo(silent_meeting=False),
            transcript_url="https://example.com/transcript",
            dateString="2023-01-01T00:00:00Z",
            meeting_attendees=[],
            speakers=[],
        )

    @staticmethod
    def create_event_envelope(
        event_type: str = "test.event.created",
        payload: Any = None,
        event_id: UUID = None,
        correlation_ids: List[UUID] = None,
    ) -> EventEnvelope:
        """Create a test EventEnvelope."""
        if payload is None:
            payload = {"test": "data"}

        return EventEnvelope(
            event_id=event_id or uuid4(),
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            version="2.0.0",
            source=EventFactory.create_source(),
            correlation_ids=correlation_ids or [],
            payload=payload,
        )


@pytest.fixture
def event_factory():
    """Provide EventFactory instance."""
    return EventFactory()


@pytest.fixture
async def fake_redis():
    """Provide a fake Redis instance for testing."""
    redis_instance = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield redis_instance
    await redis_instance.aclose()


@pytest.fixture
async def correlation_tracker(fake_redis):
    """Provide a CorrelationTracker instance with fake Redis."""
    tracker = CorrelationTracker(
        redis_host="localhost",
        redis_port=6379,
        ttl_days=30,
        max_retries=3,
        connection_timeout=5.0,
    )

    # Inject fake Redis
    with patch("correlation_tracker.redis.from_url", return_value=fake_redis):
        await tracker.start()

    yield tracker

    await tracker.close()


@pytest.fixture
async def mock_rabbitmq():
    """Mock RabbitMQ connection for Publisher tests."""
    mock_conn = AsyncMock()
    mock_channel = AsyncMock()
    mock_exchange = AsyncMock()

    mock_channel.is_closed = False
    mock_conn.channel.return_value = mock_channel
    mock_channel.declare_exchange.return_value = mock_exchange

    return {
        "conn": mock_conn,
        "channel": mock_channel,
        "exchange": mock_exchange,
    }


@pytest.fixture
async def publisher_with_tracking(fake_redis, mock_rabbitmq):
    """Provide a Publisher instance with correlation tracking enabled."""
    publisher = Publisher(enable_correlation_tracking=True)

    # Mock RabbitMQ connection
    with patch("rabbit.aio_pika.connect_robust", return_value=mock_rabbitmq["conn"]):
        with patch("correlation_tracker.redis.from_url", return_value=fake_redis):
            await publisher.start()

    yield publisher

    await publisher.close()


@pytest.fixture
async def publisher_without_tracking(mock_rabbitmq):
    """Provide a Publisher instance without correlation tracking."""
    publisher = Publisher(enable_correlation_tracking=False)

    # Mock RabbitMQ connection
    with patch("rabbit.aio_pika.connect_robust", return_value=mock_rabbitmq["conn"]):
        await publisher.start()

    yield publisher

    await publisher.close()


# ============================================================================
# Test Suite 1: CorrelationTracker Initialization and Connection
# ============================================================================


class TestCorrelationTrackerInitialization:
    """Test CorrelationTracker initialization and connection management."""

    @pytest.mark.asyncio
    async def test_initialization_with_defaults(self):
        """Test tracker can be initialized with default parameters."""
        tracker = CorrelationTracker()

        assert tracker.redis_url == "redis://localhost:6379/0"
        assert tracker.redis_password is None
        assert tracker.ttl_seconds == 30 * 86400
        assert tracker.max_retries == 3
        assert tracker.connection_timeout == 5.0
        assert tracker.redis is None
        assert tracker._started is False

    @pytest.mark.asyncio
    async def test_initialization_with_custom_params(self):
        """Test tracker initialization with custom parameters."""
        tracker = CorrelationTracker(
            redis_host="custom-host",
            redis_port=6380,
            redis_db=2,
            redis_password="secret",
            ttl_days=7,
            max_retries=5,
            connection_timeout=10.0,
        )

        assert tracker.redis_url == "redis://custom-host:6380/2"
        assert tracker.redis_password == "secret"
        assert tracker.ttl_seconds == 7 * 86400
        assert tracker.max_retries == 5
        assert tracker.connection_timeout == 10.0

    @pytest.mark.asyncio
    async def test_successful_connection(self, correlation_tracker):
        """Test successful Redis connection."""
        assert correlation_tracker._started is True
        assert correlation_tracker.redis is not None

    @pytest.mark.asyncio
    async def test_connection_failure_graceful_degradation(self):
        """Test graceful degradation when Redis is unavailable."""
        tracker = CorrelationTracker(redis_host="nonexistent-host", connection_timeout=0.1)

        # Mock redis.from_url to raise an error
        async def mock_from_url(*args, **kwargs):
            mock_redis = AsyncMock()
            mock_redis.ping = AsyncMock(side_effect=ConnectionError("Connection refused"))
            return mock_redis

        with patch("correlation_tracker.redis.from_url", side_effect=mock_from_url):
            await tracker.start()

        # Should not raise, but should not be started
        assert tracker._started is False
        assert tracker.redis is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, correlation_tracker):
        """Test that calling start() multiple times is safe."""
        # Start again (already started in fixture)
        await correlation_tracker.start()
        await correlation_tracker.start()

        assert correlation_tracker._started is True

    @pytest.mark.asyncio
    async def test_close_connection(self, fake_redis):
        """Test closing Redis connection."""
        tracker = CorrelationTracker()

        with patch("correlation_tracker.redis.from_url", return_value=fake_redis):
            await tracker.start()

        assert tracker._started is True

        await tracker.close()

        assert tracker._started is False
        assert tracker.redis is None


# ============================================================================
# Test Suite 2: Deterministic Event ID Generation
# ============================================================================


class TestDeterministicEventIDGeneration:
    """Test deterministic UUID generation for idempotency."""

    def test_same_inputs_produce_same_uuid(self, correlation_tracker):
        """Test that same inputs always generate the same UUID."""
        event_id_1 = correlation_tracker.generate_event_id(
            event_type="fireflies.transcript.upload",
            unique_key="meeting_abc123",
        )

        event_id_2 = correlation_tracker.generate_event_id(
            event_type="fireflies.transcript.upload",
            unique_key="meeting_abc123",
        )

        assert event_id_1 == event_id_2
        assert isinstance(event_id_1, UUID)

    def test_different_event_types_produce_different_uuids(self, correlation_tracker):
        """Test that different event types produce different UUIDs."""
        event_id_1 = correlation_tracker.generate_event_id(
            event_type="fireflies.transcript.upload",
            unique_key="meeting_abc123",
        )

        event_id_2 = correlation_tracker.generate_event_id(
            event_type="fireflies.transcript.ready",
            unique_key="meeting_abc123",
        )

        assert event_id_1 != event_id_2

    def test_different_unique_keys_produce_different_uuids(self, correlation_tracker):
        """Test that different unique keys produce different UUIDs."""
        event_id_1 = correlation_tracker.generate_event_id(
            event_type="fireflies.transcript.upload",
            unique_key="meeting_abc123",
        )

        event_id_2 = correlation_tracker.generate_event_id(
            event_type="fireflies.transcript.upload",
            unique_key="meeting_xyz789",
        )

        assert event_id_1 != event_id_2

    def test_different_namespaces_produce_different_uuids(self, correlation_tracker):
        """Test that different namespaces produce different UUIDs."""
        event_id_1 = correlation_tracker.generate_event_id(
            event_type="test.event",
            unique_key="key_123",
            namespace="bloodbank",
        )

        event_id_2 = correlation_tracker.generate_event_id(
            event_type="test.event",
            unique_key="key_123",
            namespace="other_system",
        )

        assert event_id_1 != event_id_2

    def test_generate_idempotent_id_convenience_function(self, correlation_tracker):
        """Test the generate_idempotent_id convenience function."""
        event_id_1 = generate_idempotent_id(
            correlation_tracker,
            "fireflies.transcript.upload",
            meeting_id="abc123",
            user_id="user_456",
        )

        event_id_2 = generate_idempotent_id(
            correlation_tracker,
            "fireflies.transcript.upload",
            meeting_id="abc123",
            user_id="user_456",
        )

        assert event_id_1 == event_id_2

    def test_generate_idempotent_id_field_order_independence(self, correlation_tracker):
        """Test that field order doesn't affect generated UUID."""
        event_id_1 = generate_idempotent_id(
            correlation_tracker,
            "test.event",
            field_a="value_a",
            field_b="value_b",
        )

        event_id_2 = generate_idempotent_id(
            correlation_tracker,
            "test.event",
            field_b="value_b",
            field_a="value_a",
        )

        assert event_id_1 == event_id_2


# ============================================================================
# Test Suite 3: Adding Correlations
# ============================================================================


class TestAddingCorrelations:
    """Test adding correlation relationships between events."""

    @pytest.mark.asyncio
    async def test_add_single_parent_correlation(self, correlation_tracker, fake_redis):
        """Test adding a correlation with a single parent."""
        parent_id = uuid4()
        child_id = uuid4()

        await correlation_tracker.add_correlation(
            child_event_id=child_id,
            parent_event_ids=[parent_id],
        )

        # Verify forward mapping (child -> parent)
        forward_key = f"bloodbank:correlation:forward:{str(child_id)}"
        data = await fake_redis.get(forward_key)
        assert data is not None

        correlation = orjson.loads(data)
        assert correlation["parent_event_ids"] == [str(parent_id)]

        # Verify reverse mapping (parent -> child)
        reverse_key = f"bloodbank:correlation:reverse:{str(parent_id)}"
        children = await fake_redis.smembers(reverse_key)
        assert str(child_id) in children

    @pytest.mark.asyncio
    async def test_add_multiple_parent_correlation(self, correlation_tracker, fake_redis):
        """Test adding a correlation with multiple parents."""
        parent_id_1 = uuid4()
        parent_id_2 = uuid4()
        parent_id_3 = uuid4()
        child_id = uuid4()

        await correlation_tracker.add_correlation(
            child_event_id=child_id,
            parent_event_ids=[parent_id_1, parent_id_2, parent_id_3],
        )

        # Verify forward mapping
        forward_key = f"bloodbank:correlation:forward:{str(child_id)}"
        data = await fake_redis.get(forward_key)
        correlation = orjson.loads(data)

        assert len(correlation["parent_event_ids"]) == 3
        assert str(parent_id_1) in correlation["parent_event_ids"]
        assert str(parent_id_2) in correlation["parent_event_ids"]
        assert str(parent_id_3) in correlation["parent_event_ids"]

        # Verify reverse mappings for all parents
        for parent_id in [parent_id_1, parent_id_2, parent_id_3]:
            reverse_key = f"bloodbank:correlation:reverse:{str(parent_id)}"
            children = await fake_redis.smembers(reverse_key)
            assert str(child_id) in children

    @pytest.mark.asyncio
    async def test_add_correlation_with_metadata(self, correlation_tracker, fake_redis):
        """Test adding a correlation with custom metadata."""
        parent_id = uuid4()
        child_id = uuid4()
        metadata = {
            "reason": "transcript_processing",
            "stage": "vectorization",
            "custom_field": "custom_value",
        }

        await correlation_tracker.add_correlation(
            child_event_id=child_id,
            parent_event_ids=[parent_id],
            metadata=metadata,
        )

        # Verify metadata is stored
        forward_key = f"bloodbank:correlation:forward:{str(child_id)}"
        data = await fake_redis.get(forward_key)
        correlation = orjson.loads(data)

        assert correlation["metadata"] == metadata

    @pytest.mark.asyncio
    async def test_add_correlation_sets_ttl(self, correlation_tracker, fake_redis):
        """Test that correlation data has TTL set."""
        parent_id = uuid4()
        child_id = uuid4()

        await correlation_tracker.add_correlation(
            child_event_id=child_id,
            parent_event_ids=[parent_id],
        )

        # Check TTL on forward key
        forward_key = f"bloodbank:correlation:forward:{str(child_id)}"
        ttl = await fake_redis.ttl(forward_key)
        assert ttl > 0
        assert ttl <= correlation_tracker.ttl_seconds

        # Check TTL on reverse key
        reverse_key = f"bloodbank:correlation:reverse:{str(parent_id)}"
        ttl = await fake_redis.ttl(reverse_key)
        assert ttl > 0
        assert ttl <= correlation_tracker.ttl_seconds

    @pytest.mark.asyncio
    async def test_add_correlation_when_not_started(self):
        """Test that add_correlation degrades gracefully when tracker not started."""
        tracker = CorrelationTracker()
        # Don't call start()

        parent_id = uuid4()
        child_id = uuid4()

        # Should not raise an exception
        await tracker.add_correlation(
            child_event_id=child_id,
            parent_event_ids=[parent_id],
        )

    @pytest.mark.asyncio
    async def test_link_events_convenience_function(self, correlation_tracker, fake_redis):
        """Test the link_events convenience function."""
        parent_id = uuid4()
        child_id = uuid4()

        await link_events(
            correlation_tracker,
            parent=parent_id,
            child=child_id,
            reason="test_linking",
        )

        # Verify correlation was created
        forward_key = f"bloodbank:correlation:forward:{str(child_id)}"
        data = await fake_redis.get(forward_key)
        correlation = orjson.loads(data)

        assert correlation["parent_event_ids"] == [str(parent_id)]
        assert correlation["metadata"]["reason"] == "test_linking"


# ============================================================================
# Test Suite 4: Querying Correlation Chains
# ============================================================================


class TestQueryingCorrelationChains:
    """Test querying parent/child relationships and correlation chains."""

    @pytest.mark.asyncio
    async def test_get_parents_single_parent(self, correlation_tracker):
        """Test getting immediate parents with single parent."""
        parent_id = uuid4()
        child_id = uuid4()

        await correlation_tracker.add_correlation(child_id, [parent_id])

        parents = await correlation_tracker.get_parents(child_id)

        assert len(parents) == 1
        assert parents[0] == parent_id

    @pytest.mark.asyncio
    async def test_get_parents_multiple_parents(self, correlation_tracker):
        """Test getting immediate parents with multiple parents."""
        parent_id_1 = uuid4()
        parent_id_2 = uuid4()
        child_id = uuid4()

        await correlation_tracker.add_correlation(child_id, [parent_id_1, parent_id_2])

        parents = await correlation_tracker.get_parents(child_id)

        assert len(parents) == 2
        assert parent_id_1 in parents
        assert parent_id_2 in parents

    @pytest.mark.asyncio
    async def test_get_parents_no_parents(self, correlation_tracker):
        """Test getting parents for an event with no parents."""
        event_id = uuid4()

        parents = await correlation_tracker.get_parents(event_id)

        assert parents == []

    @pytest.mark.asyncio
    async def test_get_children_single_child(self, correlation_tracker):
        """Test getting immediate children with single child."""
        parent_id = uuid4()
        child_id = uuid4()

        await correlation_tracker.add_correlation(child_id, [parent_id])

        children = await correlation_tracker.get_children(parent_id)

        assert len(children) == 1
        assert children[0] == child_id

    @pytest.mark.asyncio
    async def test_get_children_multiple_children(self, correlation_tracker):
        """Test getting immediate children with multiple children."""
        parent_id = uuid4()
        child_id_1 = uuid4()
        child_id_2 = uuid4()
        child_id_3 = uuid4()

        await correlation_tracker.add_correlation(child_id_1, [parent_id])
        await correlation_tracker.add_correlation(child_id_2, [parent_id])
        await correlation_tracker.add_correlation(child_id_3, [parent_id])

        children = await correlation_tracker.get_children(parent_id)

        assert len(children) == 3
        assert child_id_1 in children
        assert child_id_2 in children
        assert child_id_3 in children

    @pytest.mark.asyncio
    async def test_get_children_no_children(self, correlation_tracker):
        """Test getting children for an event with no children."""
        event_id = uuid4()

        children = await correlation_tracker.get_children(event_id)

        assert children == []

    @pytest.mark.asyncio
    async def test_get_correlation_chain_ancestors_linear(self, correlation_tracker):
        """Test getting ancestor chain for linear event sequence: A -> B -> C -> D."""
        event_a = uuid4()
        event_b = uuid4()
        event_c = uuid4()
        event_d = uuid4()

        # Build chain: A -> B -> C -> D
        await correlation_tracker.add_correlation(event_b, [event_a])
        await correlation_tracker.add_correlation(event_c, [event_b])
        await correlation_tracker.add_correlation(event_d, [event_c])

        # Query ancestors from D
        ancestors = await correlation_tracker.get_correlation_chain(event_d, "ancestors")

        # Should include A, B, C, D (in topological order)
        assert len(ancestors) == 4
        assert event_d in ancestors
        assert event_c in ancestors
        assert event_b in ancestors
        assert event_a in ancestors

    @pytest.mark.asyncio
    async def test_get_correlation_chain_descendants_linear(self, correlation_tracker):
        """Test getting descendant chain for linear event sequence: A -> B -> C -> D."""
        event_a = uuid4()
        event_b = uuid4()
        event_c = uuid4()
        event_d = uuid4()

        # Build chain: A -> B -> C -> D
        await correlation_tracker.add_correlation(event_b, [event_a])
        await correlation_tracker.add_correlation(event_c, [event_b])
        await correlation_tracker.add_correlation(event_d, [event_c])

        # Query descendants from A
        descendants = await correlation_tracker.get_correlation_chain(event_a, "descendants")

        # Should include A, B, C, D
        assert len(descendants) == 4
        assert event_a in descendants
        assert event_b in descendants
        assert event_c in descendants
        assert event_d in descendants

    @pytest.mark.asyncio
    async def test_get_correlation_chain_ancestors_branching(self, correlation_tracker):
        """Test ancestor chain with branching: A -> B -> D, C -> D."""
        event_a = uuid4()
        event_b = uuid4()
        event_c = uuid4()
        event_d = uuid4()

        # Build branching chain
        await correlation_tracker.add_correlation(event_b, [event_a])
        await correlation_tracker.add_correlation(event_d, [event_b, event_c])

        # Query ancestors from D
        ancestors = await correlation_tracker.get_correlation_chain(event_d, "ancestors")

        # Should include A, B, C, D
        assert len(ancestors) == 4
        assert event_a in ancestors
        assert event_b in ancestors
        assert event_c in ancestors
        assert event_d in ancestors

    @pytest.mark.asyncio
    async def test_get_correlation_chain_descendants_branching(self, correlation_tracker):
        """Test descendant chain with branching: A -> B, A -> C."""
        event_a = uuid4()
        event_b = uuid4()
        event_c = uuid4()

        # Build branching chain
        await correlation_tracker.add_correlation(event_b, [event_a])
        await correlation_tracker.add_correlation(event_c, [event_a])

        # Query descendants from A
        descendants = await correlation_tracker.get_correlation_chain(event_a, "descendants")

        # Should include A, B, C
        assert len(descendants) == 3
        assert event_a in descendants
        assert event_b in descendants
        assert event_c in descendants

    @pytest.mark.asyncio
    async def test_get_correlation_chain_max_depth(self, correlation_tracker):
        """Test that max_depth prevents infinite loops."""
        event_ids = [uuid4() for _ in range(10)]

        # Build long chain
        for i in range(len(event_ids) - 1):
            await correlation_tracker.add_correlation(event_ids[i + 1], [event_ids[i]])

        # Query with limited depth
        ancestors = await correlation_tracker.get_correlation_chain(
            event_ids[-1], "ancestors", max_depth=5
        )

        # Should stop at max_depth
        assert len(ancestors) <= 5

    @pytest.mark.asyncio
    async def test_get_correlation_chain_no_correlations(self, correlation_tracker):
        """Test getting chain for event with no correlations."""
        event_id = uuid4()

        ancestors = await correlation_tracker.get_correlation_chain(event_id, "ancestors")
        descendants = await correlation_tracker.get_correlation_chain(event_id, "descendants")

        assert ancestors == []
        assert descendants == []


# ============================================================================
# Test Suite 5: Graceful Degradation When Redis Unavailable
# ============================================================================


class TestGracefulDegradation:
    """Test that system degrades gracefully when Redis is unavailable."""

    @pytest.mark.asyncio
    async def test_operations_when_tracker_not_started(self):
        """Test all operations work (no-op) when tracker not started."""
        tracker = CorrelationTracker()
        # Don't call start()

        parent_id = uuid4()
        child_id = uuid4()

        # All operations should not raise exceptions
        await tracker.add_correlation(child_id, [parent_id])

        parents = await tracker.get_parents(child_id)
        assert parents == []

        children = await tracker.get_children(parent_id)
        assert children == []

        chain = await tracker.get_correlation_chain(child_id, "ancestors")
        assert chain == []

        metadata = await tracker.get_correlation_metadata(child_id)
        assert metadata is None

        debug = await tracker.debug_dump(child_id)
        assert debug["parents"] == []
        assert debug["children"] == []

    @pytest.mark.asyncio
    async def test_operations_after_redis_failure(self, correlation_tracker, fake_redis):
        """Test operations continue gracefully after Redis fails."""
        parent_id = uuid4()
        child_id = uuid4()

        # Successful operation
        await correlation_tracker.add_correlation(child_id, [parent_id])

        # Simulate Redis failure
        await fake_redis.aclose()
        correlation_tracker.redis = None
        correlation_tracker._started = False

        # Operations should not raise
        await correlation_tracker.add_correlation(uuid4(), [uuid4()])

        parents = await correlation_tracker.get_parents(child_id)
        assert parents == []

    @pytest.mark.asyncio
    async def test_get_correlation_metadata_when_not_started(self):
        """Test getting metadata when tracker not started."""
        tracker = CorrelationTracker()

        metadata = await tracker.get_correlation_metadata(uuid4())

        assert metadata is None

    @pytest.mark.asyncio
    async def test_debug_dump_when_not_started(self):
        """Test debug dump when tracker not started returns empty data."""
        tracker = CorrelationTracker()
        event_id = uuid4()

        debug = await tracker.debug_dump(event_id)

        assert debug["event_id"] == str(event_id)
        assert debug["parents"] == []
        assert debug["children"] == []
        assert debug["ancestors"] == []
        assert debug["descendants"] == []
        assert debug["metadata"] is None


# ============================================================================
# Test Suite 6: Publisher Integration with Correlation Tracking
# ============================================================================


class TestPublisherIntegration:
    """Test Publisher integration with correlation tracking enabled/disabled."""

    @pytest.mark.asyncio
    async def test_publisher_without_tracking_initialization(self, publisher_without_tracking):
        """Test Publisher initializes correctly without correlation tracking."""
        assert publisher_without_tracking.enable_correlation_tracking is False
        assert publisher_without_tracking.tracker is None
        assert publisher_without_tracking._started is True

    @pytest.mark.asyncio
    async def test_publisher_with_tracking_initialization(self, publisher_with_tracking):
        """Test Publisher initializes correctly with correlation tracking."""
        assert publisher_with_tracking.enable_correlation_tracking is True
        assert publisher_with_tracking.tracker is not None
        assert publisher_with_tracking._started is True

    @pytest.mark.asyncio
    async def test_publish_without_tracking(
        self, publisher_without_tracking, event_factory, mock_rabbitmq
    ):
        """Test publishing events without correlation tracking."""
        payload = event_factory.create_fireflies_payload()
        envelope = event_factory.create_event_envelope(
            event_type="fireflies.transcript.ready",
            payload=payload,
        )

        # Should publish successfully without correlation tracking
        await publisher_without_tracking.publish(
            routing_key="fireflies.transcript.ready",
            body=envelope.model_dump(mode="json"),
        )

        # Verify message was published
        mock_rabbitmq["exchange"].publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_with_tracking_no_correlation(
        self, publisher_with_tracking, event_factory, mock_rabbitmq
    ):
        """Test publishing event with tracking enabled but no parent events."""
        payload = event_factory.create_fireflies_payload()
        envelope = event_factory.create_event_envelope(
            event_type="fireflies.transcript.ready",
            payload=payload,
        )

        await publisher_with_tracking.publish(
            routing_key="fireflies.transcript.ready",
            body=envelope.model_dump(mode="json"),
        )

        # Should publish successfully
        mock_rabbitmq["exchange"].publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_with_correlation(
        self, publisher_with_tracking, event_factory, mock_rabbitmq
    ):
        """Test publishing event with correlation to parent events."""
        parent_event_id = uuid4()
        child_event_id = uuid4()

        payload = event_factory.create_fireflies_payload()
        envelope = event_factory.create_event_envelope(
            event_type="fireflies.transcript.processed",
            payload=payload,
            event_id=child_event_id,
        )

        await publisher_with_tracking.publish(
            routing_key="fireflies.transcript.processed",
            body=envelope.model_dump(mode="json"),
            event_id=child_event_id,
            parent_event_ids=[parent_event_id],
        )

        # Verify message was published
        mock_rabbitmq["exchange"].publish.assert_called_once()

        # Verify correlation was tracked
        parents = await publisher_with_tracking.tracker.get_parents(child_event_id)
        assert len(parents) == 1
        assert parents[0] == parent_event_id

    @pytest.mark.asyncio
    async def test_publish_with_multiple_parent_correlations(
        self, publisher_with_tracking, event_factory, mock_rabbitmq
    ):
        """Test publishing event correlated to multiple parents."""
        parent_id_1 = uuid4()
        parent_id_2 = uuid4()
        child_event_id = uuid4()

        payload = event_factory.create_fireflies_payload()
        envelope = event_factory.create_event_envelope(
            event_type="fireflies.transcript.merged",
            payload=payload,
            event_id=child_event_id,
        )

        await publisher_with_tracking.publish(
            routing_key="fireflies.transcript.merged",
            body=envelope.model_dump(mode="json"),
            event_id=child_event_id,
            parent_event_ids=[parent_id_1, parent_id_2],
        )

        # Verify correlations
        parents = await publisher_with_tracking.tracker.get_parents(child_event_id)
        assert len(parents) == 2
        assert parent_id_1 in parents
        assert parent_id_2 in parents

    @pytest.mark.asyncio
    async def test_generate_event_id_with_tracking(self, publisher_with_tracking):
        """Test generating deterministic event ID via Publisher."""
        event_id_1 = publisher_with_tracking.generate_event_id(
            "fireflies.transcript.upload",
            meeting_id="abc123",
        )

        event_id_2 = publisher_with_tracking.generate_event_id(
            "fireflies.transcript.upload",
            meeting_id="abc123",
        )

        assert event_id_1 == event_id_2
        assert isinstance(event_id_1, UUID)

    @pytest.mark.asyncio
    async def test_generate_event_id_without_tracking_raises(self, publisher_without_tracking):
        """Test that generate_event_id raises when tracking disabled."""
        with pytest.raises(RuntimeError, match="Correlation tracking is disabled"):
            publisher_without_tracking.generate_event_id(
                "test.event",
                key="value",
            )

    @pytest.mark.asyncio
    async def test_get_correlation_chain_with_tracking(self, publisher_with_tracking):
        """Test getting correlation chain via Publisher."""
        event_a = uuid4()
        event_b = uuid4()
        event_c = uuid4()

        # Build chain
        await publisher_with_tracking.tracker.add_correlation(event_b, [event_a])
        await publisher_with_tracking.tracker.add_correlation(event_c, [event_b])

        # Query via Publisher
        chain = await publisher_with_tracking.get_correlation_chain(event_c, "ancestors")

        assert len(chain) == 3
        assert event_a in chain
        assert event_b in chain
        assert event_c in chain

    @pytest.mark.asyncio
    async def test_get_correlation_chain_without_tracking_raises(
        self, publisher_without_tracking
    ):
        """Test that get_correlation_chain raises when tracking disabled."""
        with pytest.raises(RuntimeError, match="Correlation tracking is disabled"):
            await publisher_without_tracking.get_correlation_chain(uuid4())

    @pytest.mark.asyncio
    async def test_publish_extracts_event_id_from_body(
        self, publisher_with_tracking, mock_rabbitmq
    ):
        """Test that Publisher extracts event_id from message body."""
        event_id = uuid4()
        parent_id = uuid4()

        body = {
            "event_id": str(event_id),
            "event_type": "test.event",
            "payload": {"test": "data"},
        }

        await publisher_with_tracking.publish(
            routing_key="test.event",
            body=body,
            parent_event_ids=[parent_id],
        )

        # Verify correlation was tracked using extracted event_id
        parents = await publisher_with_tracking.tracker.get_parents(event_id)
        assert len(parents) == 1
        assert parents[0] == parent_id

    @pytest.mark.asyncio
    async def test_publish_with_correlation_metadata(
        self, publisher_with_tracking, mock_rabbitmq
    ):
        """Test publishing with correlation metadata."""
        parent_id = uuid4()
        child_id = uuid4()

        metadata = {
            "processing_stage": "vectorization",
            "retry_count": 0,
        }

        await publisher_with_tracking.publish(
            routing_key="test.event",
            body={"event_id": str(child_id), "data": "test"},
            event_id=child_id,
            parent_event_ids=[parent_id],
            correlation_metadata=metadata,
        )

        # Verify metadata was stored
        stored_metadata = await publisher_with_tracking.tracker.get_correlation_metadata(
            child_id
        )
        assert stored_metadata == metadata


# ============================================================================
# Test Suite 7: Debug Endpoints
# ============================================================================


class TestDebugEndpoints:
    """Test debug functionality returns correct data."""

    @pytest.mark.asyncio
    async def test_debug_dump_basic(self, correlation_tracker):
        """Test basic debug_dump with parent and child relationships."""
        parent_id = uuid4()
        event_id = uuid4()
        child_id = uuid4()

        # Build relationships
        await correlation_tracker.add_correlation(event_id, [parent_id])
        await correlation_tracker.add_correlation(child_id, [event_id])

        # Get debug dump
        debug = await correlation_tracker.debug_dump(event_id)

        assert debug["event_id"] == str(event_id)
        assert str(parent_id) in debug["parents"]
        assert str(child_id) in debug["children"]
        assert str(parent_id) in debug["ancestors"]
        assert str(child_id) in debug["descendants"]

    @pytest.mark.asyncio
    async def test_debug_dump_with_metadata(self, correlation_tracker):
        """Test debug_dump includes metadata."""
        parent_id = uuid4()
        child_id = uuid4()
        metadata = {"reason": "test", "stage": "processing"}

        await correlation_tracker.add_correlation(
            child_id, [parent_id], metadata=metadata
        )

        debug = await correlation_tracker.debug_dump(child_id)

        assert debug["metadata"] == metadata

    @pytest.mark.asyncio
    async def test_debug_dump_complex_chain(self, correlation_tracker):
        """Test debug_dump with complex chain structure."""
        # Build complex chain: A -> B -> D, C -> D -> E
        event_a = uuid4()
        event_b = uuid4()
        event_c = uuid4()
        event_d = uuid4()
        event_e = uuid4()

        await correlation_tracker.add_correlation(event_b, [event_a])
        await correlation_tracker.add_correlation(event_d, [event_b, event_c])
        await correlation_tracker.add_correlation(event_e, [event_d])

        # Debug dump for D
        debug = await correlation_tracker.debug_dump(event_d)

        assert len(debug["parents"]) == 2
        assert str(event_b) in debug["parents"]
        assert str(event_c) in debug["parents"]

        assert len(debug["children"]) == 1
        assert str(event_e) in debug["children"]

        # Ancestors should include A, B, C
        assert str(event_a) in debug["ancestors"]
        assert str(event_b) in debug["ancestors"]
        assert str(event_c) in debug["ancestors"]

        # Descendants should include E
        assert str(event_e) in debug["descendants"]

    @pytest.mark.asyncio
    async def test_debug_correlation_via_publisher(self, publisher_with_tracking):
        """Test debug_correlation method on Publisher."""
        parent_id = uuid4()
        child_id = uuid4()

        await publisher_with_tracking.tracker.add_correlation(
            child_id, [parent_id], metadata={"test": "data"}
        )

        debug = await publisher_with_tracking.debug_correlation(child_id)

        assert debug["event_id"] == str(child_id)
        assert str(parent_id) in debug["parents"]
        assert debug["metadata"] == {"test": "data"}

    @pytest.mark.asyncio
    async def test_debug_correlation_without_tracking_raises(self, publisher_without_tracking):
        """Test that debug_correlation raises when tracking disabled."""
        with pytest.raises(RuntimeError, match="Correlation tracking is disabled"):
            await publisher_without_tracking.debug_correlation(uuid4())

    @pytest.mark.asyncio
    async def test_get_correlation_metadata(self, correlation_tracker):
        """Test getting correlation metadata directly."""
        parent_id = uuid4()
        child_id = uuid4()
        metadata = {
            "processing_stage": "upload",
            "retry_count": 2,
            "custom_field": "custom_value",
        }

        await correlation_tracker.add_correlation(
            child_id, [parent_id], metadata=metadata
        )

        retrieved_metadata = await correlation_tracker.get_correlation_metadata(child_id)

        assert retrieved_metadata == metadata

    @pytest.mark.asyncio
    async def test_get_correlation_metadata_not_found(self, correlation_tracker):
        """Test getting metadata for non-existent correlation."""
        event_id = uuid4()

        metadata = await correlation_tracker.get_correlation_metadata(event_id)

        assert metadata is None


# ============================================================================
# Test Suite 8: Edge Cases and Error Handling
# ============================================================================


class TestEdgeCasesAndErrorHandling:
    """Test edge cases and error handling scenarios."""

    @pytest.mark.asyncio
    async def test_correlation_with_empty_parent_list(self, correlation_tracker):
        """Test adding correlation with empty parent list."""
        child_id = uuid4()

        # Should not raise
        await correlation_tracker.add_correlation(child_id, [])

        parents = await correlation_tracker.get_parents(child_id)
        assert parents == []

    @pytest.mark.asyncio
    async def test_circular_correlation_detection(self, correlation_tracker):
        """Test handling of circular correlations with max_depth."""
        event_a = uuid4()
        event_b = uuid4()

        # Create correlations (not truly circular, but tests depth limit)
        await correlation_tracker.add_correlation(event_b, [event_a])
        await correlation_tracker.add_correlation(event_a, [event_b])

        # Should handle gracefully with max_depth
        chain = await correlation_tracker.get_correlation_chain(
            event_a, "ancestors", max_depth=10
        )

        # Should stop at max_depth
        assert len(chain) <= 10

    @pytest.mark.asyncio
    async def test_concurrent_correlation_additions(self, correlation_tracker):
        """Test adding correlations concurrently."""
        parent_id = uuid4()
        child_ids = [uuid4() for _ in range(10)]

        # Add correlations concurrently
        tasks = [
            correlation_tracker.add_correlation(child_id, [parent_id])
            for child_id in child_ids
        ]
        await asyncio.gather(*tasks)

        # Verify all children were added
        children = await correlation_tracker.get_children(parent_id)
        assert len(children) == 10
        for child_id in child_ids:
            assert child_id in children

    @pytest.mark.asyncio
    async def test_large_correlation_chain(self, correlation_tracker):
        """Test handling large correlation chains."""
        chain_length = 50
        event_ids = [uuid4() for _ in range(chain_length)]

        # Build long chain
        for i in range(chain_length - 1):
            await correlation_tracker.add_correlation(event_ids[i + 1], [event_ids[i]])

        # Query full chain
        ancestors = await correlation_tracker.get_correlation_chain(
            event_ids[-1], "ancestors", max_depth=100
        )

        assert len(ancestors) == chain_length

    @pytest.mark.asyncio
    async def test_publisher_close_cleanup(self, publisher_with_tracking):
        """Test that Publisher.close() properly cleans up resources."""
        assert publisher_with_tracking._started is True

        await publisher_with_tracking.close()

        assert publisher_with_tracking._started is False
        assert publisher_with_tracking._conn is None
        assert publisher_with_tracking._channel is None
        assert publisher_with_tracking._exchange is None

    @pytest.mark.asyncio
    async def test_correlation_tracking_timeout_handling(
        self, publisher_with_tracking, mock_rabbitmq
    ):
        """Test that correlation tracking timeouts don't block publishing."""
        parent_id = uuid4()
        child_id = uuid4()

        # Mock tracker to timeout
        async def slow_add_correlation(*args, **kwargs):
            await asyncio.sleep(5)  # Longer than 1s timeout in publish()

        publisher_with_tracking.tracker.add_correlation = slow_add_correlation

        # Publish should succeed despite correlation timeout
        await publisher_with_tracking.publish(
            routing_key="test.event",
            body={"event_id": str(child_id), "data": "test"},
            event_id=child_id,
            parent_event_ids=[parent_id],
        )

        # Message should still be published
        mock_rabbitmq["exchange"].publish.assert_called_once()


# ============================================================================
# Run Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
