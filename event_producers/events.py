from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Literal, TypeVar, Generic
from datetime import datetime, timezone
import uuid

# --- Generic Event Envelope ---

T = TypeVar('T')

class Source(BaseModel):
    component: str
    host_id: str
    session_id: Optional[str] = None

class AgentContext(BaseModel):
    agent_instance_id: Optional[str] = None
    agent_template_id: Optional[str] = None
    task_id: Optional[str] = None

class EventEnvelope(BaseModel, Generic[T]):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str  # e.g., imi.worktree.created
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"
    source: Source
    correlation_id: Optional[uuid.UUID] = None
    agent_context: Optional[AgentContext] = None
    payload: T

# --- Event Payloads ---

# --- LLM Interaction Events ---

class ModelInfo(BaseModel):
    provider: Literal["openai", "anthropic", "google"]
    model_name: str
    temperature: float

class Prompt(BaseModel):
    system_prompt: str
    user_prompt: str
    context_injected: List[str]

class LLMInteractionPromptPayload(BaseModel):
    interaction_id: uuid.UUID
    model_info: ModelInfo
    prompt: Prompt
    tools_available: List[str]

class UsageMetrics(BaseModel):
    input_tokens: int
    output_tokens: int
    latency_ms: int

class LLMInteractionResponsePayload(BaseModel):
    interaction_id: uuid.UUID
    response_content: str
    tool_calls: List[Any]  # Can be defined more strictly later
    usage_metrics: UsageMetrics
    status: Literal["success", "failure", "truncated"]

# --- iMi (Git and Worktree) Events ---

class Repository(BaseModel):
    name: str
    url: str

class LocalPaths(BaseModel):
    imi_path: str
    trunk_path: str

class GitInfo(BaseModel):
    default_branch: str
    commit_hash: str

class IMIRepositoryClonedPayload(BaseModel):
    repository: Repository
    local_paths: LocalPaths
    git_info: GitInfo

class Worktree(BaseModel):
    name: str
    type: Literal["feat", "fix", "pr", "devops", "aiops"]
    path: str
    branch_name: str

class WorktreeGitInfo(BaseModel):
    base_branch: str
    commit_hash: str

class IMIWorktreeCreatedPayload(BaseModel):
    repository_name: str
    worktree: Worktree
    git_info: WorktreeGitInfo

# --- CLI Session and Command Logging Events ---

class CLIEnvironment(BaseModel):
    user: str
    shell: str
    tty: str
    initial_directory: str

class CLISessionStartedPayload(BaseModel):
    session_id: uuid.UUID
    environment: CLIEnvironment
    context: Literal["human_interactive", "agent_execution"]

class CLICommandExecutedPayload(BaseModel):
    session_id: uuid.UUID
    command_id: uuid.UUID
    command_line: str
    working_directory: str
    timestamp_start: datetime

class CLICommandFinishedPayload(BaseModel):
    session_id: uuid.UUID
    command_id: uuid.UUID
    exit_code: int
    duration_ms: int
    output_summary: Optional[str] = None

# --- Semantic Events (REBEL/ToDo Tracking) ---

class TodoItem(BaseModel):
    description: str
    status: Literal["added", "checked", "modified", "removed"]

class SemanticTodoUpdatedPayload(BaseModel):
    source_interaction_id: uuid.UUID
    todo_item: TodoItem
    task_context: Optional[str] = None

# --- Fireflies Transcription Event ---

class AIFilters(BaseModel):
    text_cleanup: Optional[str] = None
    task: Optional[str] = None
    pricing: Optional[str] = None
    metric: Optional[str] = None
    question: Optional[str] = None
    date_and_time: Optional[str] = None
    sentiment: Optional[str] = None

class Sentence(BaseModel):
    index: int
    speaker_name: Optional[str] = None
    speaker_id: int
    raw_text: str
    start_time: float
    end_time: float
    ai_filters: AIFilters
    text: str

class FirefliesUser(BaseModel):
    user_id: str
    email: str
    integrations: List[str]
    user_groups: List[Any]
    name: str
    num_transcripts: int
    recent_transcript: str
    recent_meeting: str
    minutes_consumed: float
    is_admin: bool

class MeetingInfo(BaseModel):
    silent_meeting: bool
    summary_status: Optional[str] = None
    fred_joined: Optional[bool] = None

class FirefliesTranscriptReadyPayload(BaseModel):
    id: str
    sentences: List[Sentence]
    title: str
    host_email: str
    organizer_email: str
    user: FirefliesUser
    fireflies_users: List[Any]
    privacy: str
    participants: List[Any]
    date: int  # timestamp
    duration: float
    summary: Optional[str] = None
    meeting_info: MeetingInfo
    transcript_url: str
    dateString: str  # ISO 8601 date string
    meeting_attendees: List[Any]
    speakers: List[Any]
    calendar_id: Optional[str] = None
    cal_id: Optional[str] = None
    calendar_type: Optional[str] = None
    meeting_link: Optional[str] = None