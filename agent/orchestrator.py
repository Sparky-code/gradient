"""Local, in-process multi-agent orchestration, shaped like ACP.

ACP (Agent Communication Protocol, i-am-bee/acp) is the open agent-interop spec
this borrows its vocabulary from: an Agent Manifest for discovery, a Run as one
agent execution over input/output Messages, and MIME-typed MessageParts so any
data shape can pass through without changing the protocol. The upstream repo
archived that standalone spec on 2025-08-27 and folded it into A2A under the
Linux Foundation — but every agent registered with this Coordinator runs in the
same Python process, so there's no network boundary here and none of that
transport/migration history matters. This module keeps ACP's data shapes (a
clean way to describe "an agent, given input messages, returns output
messages") without its HTTP layer, which would be pure latency for zero benefit
at this codebase's scale.

This replaces what Band (the hackathon's coordination-layer sponsor) would have
done — named agents, explicit routing, an audit trail — with a local
equivalent: the pipeline's four local agents (policy-reclassify, then
classify, then ground and recall) are registered here instead of called
directly from loop.py, so a fifth local agent can be added later without
loop.py needing to change.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from agent import session_log


@dataclass(frozen=True)
class MessagePart:
    content: object
    content_type: str = "text/plain"


@dataclass(frozen=True)
class Message:
    role: str
    parts: list[MessagePart]


@dataclass(frozen=True)
class AgentManifest:
    """Mirrors ACP's Agent Manifest: name + description for discovery, plus the
    local handler that actually does the work (ACP would reach this over HTTP;
    here it's a direct in-process call)."""

    name: str
    description: str
    handler: Callable[[list[Message]], list[Message]]


@dataclass
class Run:
    run_id: str
    agent_name: str
    status: str  # "completed" | "failed"
    output: list[Message] = field(default_factory=list)
    error: str | None = None


class Coordinator:
    """In-process ACP-shaped agent registry and dispatcher."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentManifest] = {}

    def register(self, manifest: AgentManifest) -> None:
        self._agents[manifest.name] = manifest

    def agents(self) -> list[AgentManifest]:
        """Mirrors ACP's GET /agents discovery endpoint."""
        return list(self._agents.values())

    def create_run(self, agent_name: str, input: list[Message]) -> Run:
        """Mirrors ACP's POST /runs. Synchronous only — no streaming/await-resume,
        since there's no long-running remote work to page over here. A handler
        that raises fails only its own run (logged to the session log) rather than
        taking down the caller, the same isolation a per-agent remote call
        would give you."""
        run_id = str(uuid.uuid4())
        manifest = self._agents.get(agent_name)
        if manifest is None:
            session_log.log_session({
                "event": "orchestrator_run", "run_id": run_id, "agent_name": agent_name,
                "status": "failed", "reason": "unknown agent",
            })
            return Run(run_id=run_id, agent_name=agent_name, status="failed",
                       error=f"no such agent: {agent_name!r}")

        try:
            output = manifest.handler(input)
            session_log.log_session({
                "event": "orchestrator_run", "run_id": run_id,
                "agent_name": agent_name, "status": "completed",
            })
            return Run(run_id=run_id, agent_name=agent_name, status="completed", output=output)
        except Exception as e:
            session_log.log_session({
                "event": "orchestrator_run", "run_id": run_id, "agent_name": agent_name,
                "status": "failed", "reason": str(e),
            })
            return Run(run_id=run_id, agent_name=agent_name, status="failed", error=str(e))


def build_default_coordinator() -> Coordinator:
    """The five local agents this pipeline runs today: reclassify posts against
    the current Pioneer-promoted policy, classify them into interest plans,
    ground and recall each interest against VectorAI DB's own local memory,
    and evolve the category taxonomy itself from whatever didn't fit anywhere
    ("other"). Registering them here — instead of loop.py calling
    reclassify/planner/vectorai/taxonomy_evolver directly — is what let
    this fifth agent get added without loop.py's dispatch shape changing."""
    from agent import planner, policy, reclassify, taxonomy_evolver
    from agent.adapters import vectorai

    coordinator = Coordinator()

    def _policy_reclassify(input: list[Message]) -> list[Message]:
        posts = input[0].parts[0].content
        current_policy = policy.load_current()
        if current_policy["exemplars"]:
            posts, changes = reclassify.apply_policy(posts, current_policy)
        else:
            changes = []
        return [Message(role="agent/policy-reclassifier", parts=[
            MessagePart(
                content={"posts": posts, "changes": changes, "policy_version": current_policy["version"]},
                content_type="application/json",
            ),
        ])]

    coordinator.register(AgentManifest(
        name="policy-reclassifier",
        description="Applies the current Pioneer-promoted policy's few-shot exemplars to suppress posts that resemble past rejections.",
        handler=_policy_reclassify,
    ))

    def _classify(input: list[Message]) -> list[Message]:
        posts = input[0].parts[0].content
        plans, low_quality_count = planner.build_plans(posts)
        return [Message(role="agent/classifier", parts=[
            MessagePart(
                content={"plans": plans, "low_quality_count": low_quality_count},
                content_type="application/json",
            ),
        ])]

    coordinator.register(AgentManifest(
        name="classifier",
        description="Groups classified posts into interest plans, filtering entertainment_only.",
        handler=_classify,
    ))

    def _ground(input: list[Message]) -> list[Message]:
        task = input[0].parts[0].content
        # Batched, not per-interest — same reasoning as _recall() below: one
        # embed subprocess call for the whole set of new plans, not one per plan.
        grounding_by_interest = vectorai.ground_locally_many(
            task["query_texts"], exclude_hrefs_by_text=task["exclude_hrefs_by_text"],
        )
        return [Message(role="agent/vectorai-grounder", parts=[
            MessagePart(content=grounding_by_interest, content_type="application/json"),
        ])]

    coordinator.register(AgentManifest(
        name="vectorai-grounder",
        description="Grounds an interest in other posts the user actually saved, via VectorAI DB's own local semantic search — not a post's own content citing itself.",
        handler=_ground,
    ))

    def _recall(input: list[Message]) -> list[Message]:
        task = input[0].parts[0].content
        # Batched, not per-interest: embed_batch() pays its ~10s model-load cost
        # once for the whole set rather than once per plan (see vectorai.py).
        memory_by_interest = vectorai.recall_similar_many(
            task["query_texts"], exclude_hrefs_by_text=task["exclude_hrefs_by_text"],
        )
        return [Message(role="agent/vectorai-recaller", parts=[
            MessagePart(content=memory_by_interest, content_type="application/json"),
        ])]

    coordinator.register(AgentManifest(
        name="vectorai-recaller",
        description="Recalls past posts similar to a batch of interests from VectorAI DB's episodic memory, one interest per query.",
        handler=_recall,
    ))

    def _taxonomy_evolve(input: list[Message]) -> list[Message]:
        posts = input[0].parts[0].content  # full pass, not pre-filtered — see evolve()'s docstring
        result = taxonomy_evolver.evolve(posts)
        return [Message(role="agent/taxonomy-evolver", parts=[
            MessagePart(content=result, content_type="application/json"),
        ])]

    coordinator.register(AgentManifest(
        name="taxonomy-evolver",
        description="Detects a genuine recurring cluster among posts that didn't fit any "
                    "existing category, checks it isn't already covered, grounds it against "
                    "VectorAI DB's own local memory, and auto-promotes a new versioned category if warranted.",
        handler=_taxonomy_evolve,
    ))

    return coordinator
