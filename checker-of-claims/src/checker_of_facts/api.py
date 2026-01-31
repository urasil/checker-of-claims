"""
REST API for the Cambridge DIS Hackathon multi-agent jury debate system.

Provides endpoints for:
- Starting a debate on a claim/truth pair
- Streaming debate progress in real-time
- Getting debate results
"""
from __future__ import annotations

import asyncio
import json
import os
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any


from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from checker_of_facts.hackathon_engine import HackathonDebateEngine, _to_jsonable
from checker_of_facts.hackathon_models import (
    ClaimTruthPair,
    DebateResult,
    DebateTurn,
    ModeratorFinalVerdict,
    ModeratorSummary,
    JurorReaction,
)
from checker_of_facts.tts import synthesize_speech


# Load environment variables (search upward for .env)
load_dotenv(find_dotenv(usecwd=True) or None)

logger = logging.getLogger("checker_of_facts.api")

# In-memory storage for active debates
active_debates: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    yield
    # Cleanup on shutdown
    active_debates.clear()


app = FastAPI(
    title="FactTrace Debate API",
    description="Multi-agent jury debate system for fact-checking",
    version="1.0.0",
    lifespan=lifespan,
)

# Configure CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# Pydantic Models for Request/Response
# ═══════════════════════════════════════════════════════════════════════════════


class StartDebateRequest(BaseModel):
    """Request to start a new debate."""
    claim: str
    truth: str
    model: str = "gpt-5.2"  # Default model for all agents


class DebateStatusResponse(BaseModel):
    """Response with debate status."""
    debate_id: str
    status: str  # "pending", "running", "completed", "error"
    phase: str | None = None
    progress: int = 0
    total_phases: int = 5  # intro, round1, summary, round2, verdict


class MessageEvent(BaseModel):
    """A single debate event for streaming."""
    event_type: str  # "intro", "turn", "summary", "verdict", "error", "complete"
    persona_id: str | None = None
    persona_name: str | None = None
    content: str
    round_number: int | None = None
    turn_index: int | None = None
    is_moderator_summary: bool = False
    verdict: dict | None = None
    reactions: list[dict] | None = None
    timestamp: str


class TTSRequest(BaseModel):
    """Request for text-to-speech synthesis."""
    persona_id: str
    text: str



# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


def persona_id_to_frontend_id(backend_id: str) -> str:
    """Map backend persona IDs to frontend persona IDs."""
    mapping = {
        "skeptic": "skeptic",
        "pedant": "academic",  # Frontend uses 'academic' for pedant
        "pragmatist": "pragmatist",
        "devil_advocate": "journalist",  # Map devil's advocate to journalist
        "context_expert": "ethicist",  # Map context expert to ethicist
        "moderator": "moderator",
        "final_judge": "moderator",  # Final judge speaks through moderator
    }
    return mapping.get(backend_id, backend_id)


def frontend_id_to_backend_id(frontend_id: str) -> str:
    """Map frontend persona IDs to backend persona IDs."""
    mapping = {
        "skeptic": "skeptic",
        "academic": "pedant",
        "pragmatist": "pragmatist",
        "journalist": "devil_advocate",
        "ethicist": "context_expert",
        "moderator": "moderator",
    }
    return mapping.get(frontend_id, frontend_id)


def verdict_to_frontend_format(verdict: ModeratorFinalVerdict) -> dict:
    """Convert backend verdict to frontend JudgeVerdict format."""
    # Map backend label to frontend outcome
    outcome_map = {
        "Faithful": "faithful",
        "Mutated": "mutated",
        "Unclear": "ambiguous",
    }
    
    return {
        "outcome": outcome_map.get(verdict.label, "ambiguous"),
        "confidence": int(verdict.confidence * 100),
        "argumentsFor": " ".join(verdict.key_arguments_for_faithful) if verdict.key_arguments_for_faithful else verdict.summary,
        "argumentsAgainst": " ".join(verdict.key_arguments_for_mutated) if verdict.key_arguments_for_mutated else "",
        "finalJudgement": verdict.final_reasoning,
        "juryVotes": [],  # Will be populated during the debate
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Streaming Debate Engine
# ═══════════════════════════════════════════════════════════════════════════════


class StreamingDebateEngine(HackathonDebateEngine):
    """Extended debate engine with streaming support."""
    
    def __init__(self, event_queue: asyncio.Queue, **kwargs):
        super().__init__(verbose=False, **kwargs)  # Disable console output
        self.event_queue = event_queue
    
    async def _emit_event(self, event: dict):
        """Emit an event to the queue."""
        await self.event_queue.put(event)
    
    async def _run_async(self, pair: ClaimTruthPair) -> DebateResult:
        """Async implementation with event streaming."""
        import random
        
        # Emit moderator intro
        intro_content = (
            f"Welcome, jury. Today we examine a claim for fact-checking. "
            f"The external claim states: \"{pair.claim[:100]}...\" "
            f"Our internal fact indicates: \"{pair.truth[:100]}...\" "
            f"Let's determine: is this claim a faithful representation, or has it been mutated?"
        )
        
        await self._emit_event({
            "event_type": "intro",
            "persona_id": "moderator",
            "persona_name": "The Moderator",
            "content": intro_content,
            "round_number": 0,
            "turn_index": 0,
            "is_moderator_summary": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        # Get juror IDs
        juror_ids = list(self.registry.jurors.keys())
        
        # ═══════════════════════════════════════════════════════════
        # ROUND 1
        # ═══════════════════════════════════════════════════════════
        round1_order = juror_ids.copy()
        random.shuffle(round1_order)
        
        round1_turns: list[DebateTurn] = []
        jury_votes = []  # Track votes for the frontend
        
        for turn_idx, juror_id in enumerate(round1_order, 1):
            turn = await self._run_juror_turn(
                pair=pair,
                juror_id=juror_id,
                round_number=1,
                turn_index=turn_idx,
                prior_turns=round1_turns,
            )
            round1_turns.append(turn)
            
            # Emit turn event
            await self._emit_event({
                "event_type": "turn",
                "persona_id": persona_id_to_frontend_id(turn.persona),
                "persona_name": turn.juror_name,
                "content": turn.content,
                "round_number": 1,
                "turn_index": turn_idx,
                "is_moderator_summary": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            
            # Get reactions
            turn_reactions = await self._get_reactions(turn, juror_ids)
            turn = replace(turn, reactions=turn_reactions)
            round1_turns[-1] = turn  # Update the turn in history
            
            formatted_reactions = [
                {
                    "jurorId": persona_id_to_frontend_id(r.juror_id),
                    "jurorName": r.juror_name,
                    "reaction": r.reaction,
                    "reason": r.reason,
                }
                for r in turn_reactions
            ]
            
            await self._emit_event({
                "event_type": "reactions",
                "persona_id": None,
                "persona_name": None,
                "content": "",
                "round_number": 1,
                "turn_index": turn_idx,
                "is_moderator_summary": False,
                "reactions": formatted_reactions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        
        from checker_of_facts.hackathon_models import DebateRound
        round1 = DebateRound(
            round_number=1,
            juror_order=round1_order,
            turns=round1_turns,
        )
        
        # ═══════════════════════════════════════════════════════════
        # MODERATOR SUMMARY
        # ═══════════════════════════════════════════════════════════
        moderator_summary = await self._run_moderator_summary(pair, round1_turns)
        
        summary_content = (
            f"{moderator_summary.summary}\n\n"
            f"For round two, let's focus on: {moderator_summary.guidance_for_next_round}"
        )
        
        await self._emit_event({
            "event_type": "summary",
            "persona_id": "moderator",
            "persona_name": "The Moderator",
            "content": summary_content,
            "round_number": 1,
            "turn_index": 0,
            "is_moderator_summary": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        # ═══════════════════════════════════════════════════════════
        # ROUND 2
        # ═══════════════════════════════════════════════════════════
        round2_order = juror_ids.copy()
        random.shuffle(round2_order)
        
        all_prior_turns = round1_turns.copy()
        round2_turns: list[DebateTurn] = []
        
        for turn_idx, juror_id in enumerate(round2_order, 1):
            turn = await self._run_juror_turn(
                pair=pair,
                juror_id=juror_id,
                round_number=2,
                turn_index=turn_idx,
                prior_turns=all_prior_turns + round2_turns,
                moderator_summary=moderator_summary,
            )
            round2_turns.append(turn)
            
            # Emit turn event
            await self._emit_event({
                "event_type": "turn",
                "persona_id": persona_id_to_frontend_id(turn.persona),
                "persona_name": turn.juror_name,
                "content": turn.content,
                "round_number": 2,
                "turn_index": turn_idx,
                "is_moderator_summary": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            
            # Get reactions
            turn_reactions = await self._get_reactions(turn, juror_ids)
            turn = replace(turn, reactions=turn_reactions)
            round2_turns[-1] = turn  # Update the turn in history
            
            formatted_reactions = [
                {
                    "jurorId": persona_id_to_frontend_id(r.juror_id),
                    "jurorName": r.juror_name,
                    "reaction": r.reaction,
                    "reason": r.reason,
                }
                for r in turn_reactions
            ]
            
            await self._emit_event({
                "event_type": "reactions",
                "persona_id": None,
                "persona_name": None,
                "content": "",
                "round_number": 2,
                "turn_index": turn_idx,
                "is_moderator_summary": False,
                "reactions": formatted_reactions,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        
        round2 = DebateRound(
            round_number=2,
            juror_order=round2_order,
            turns=round2_turns,
        )
        
        # ═══════════════════════════════════════════════════════════
        # FINAL VERDICT
        # ═══════════════════════════════════════════════════════════
        final_verdict = await self._run_final_judge_verdict(
            pair=pair,
            all_turns=round1_turns + round2_turns,
            moderator_summary=moderator_summary,
        )
        
        # Create jury votes from debate turns
        all_turns = round1_turns + round2_turns
        seen_jurors = set()
        for turn in reversed(all_turns):
            if turn.persona not in seen_jurors:
                seen_jurors.add(turn.persona)
                # Infer vote from content (simplified heuristic)
                content_lower = turn.content.lower()
                if "mutated" in content_lower or "misleading" in content_lower or "distorted" in content_lower:
                    vote_verdict = "mutated"
                elif "faithful" in content_lower or "accurate" in content_lower or "correct" in content_lower:
                    vote_verdict = "faithful"
                else:
                    vote_verdict = "ambiguous"
                
                jury_votes.append({
                    "personaId": persona_id_to_frontend_id(turn.persona),
                    "verdict": vote_verdict,
                    "reasoning": turn.content[:200] + "..." if len(turn.content) > 200 else turn.content,
                })
        
        verdict_data = verdict_to_frontend_format(final_verdict)
        verdict_data["juryVotes"] = jury_votes
        
        await self._emit_event({
            "event_type": "verdict",
            "persona_id": "moderator",
            "persona_name": "The Moderator",
            "content": final_verdict.final_reasoning,
            "round_number": 2,
            "turn_index": 0,
            "is_moderator_summary": False,
            "verdict": verdict_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        # Signal completion
        await self._emit_event({
            "event_type": "complete",
            "persona_id": None,
            "persona_name": None,
            "content": "Debate completed",
            "round_number": None,
            "turn_index": None,
            "is_moderator_summary": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        return DebateResult(
            pair=pair,
            rounds=[round1, round2],
            moderator_summary=moderator_summary,
            final_verdict=final_verdict,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "FactTrace Debate API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/debate/stream")
async def stream_debate(request: StartDebateRequest):
    """
    Start a debate and stream events in real-time via Server-Sent Events.
    
    The client receives JSON events for each debate turn, allowing the UI
    to update progressively as the debate unfolds.
    """
    pair = ClaimTruthPair(claim=request.claim, truth=request.truth)
    event_queue: asyncio.Queue = asyncio.Queue()
    
    async def run_debate():
        """Background task to run the debate."""
        try:
            engine = StreamingDebateEngine(
                event_queue=event_queue,
                model=request.model,
            )
            await engine._run_async(pair)
        except Exception as e:
            await event_queue.put({
                "event_type": "error",
                "persona_id": None,
                "persona_name": None,
                "content": str(e),
                "round_number": None,
                "turn_index": None,
                "is_moderator_summary": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    
    async def event_generator():
        """Generate SSE events from the queue."""
        # Start the debate in the background
        task = asyncio.create_task(run_debate())
        
        try:
            while True:
                event = await event_queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                
                if event["event_type"] in ("complete", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@app.post("/debate/sync")
async def run_debate_sync(request: StartDebateRequest):
    """
    Run a complete debate and return the full result.
    
    This is a blocking endpoint that waits for the entire debate to complete.
    Use /debate/stream for real-time updates.
    """
    try:
        pair = ClaimTruthPair(claim=request.claim, truth=request.truth)
        engine = HackathonDebateEngine(model=request.model, verbose=False)
        result = await engine._run_async(pair)
        
        # Convert to JSON-serializable format
        return {
            "success": True,
            "result": _to_jsonable(result),
            "verdict": verdict_to_frontend_format(result.final_verdict),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/personas")
async def get_personas():
    """Get the list of available personas."""
    from checker_of_facts.hackathon_models import DEFAULT_JUROR_PERSONAS
    
    personas = []
    for p in DEFAULT_JUROR_PERSONAS:
        personas.append({
            "id": persona_id_to_frontend_id(p.id),
            "name": p.name,
            "role": p.persona,
            "description": p.description,
            "isJury": True,
        })
    
    # Add moderator
    personas.append({
        "id": "moderator",
        "name": "The Moderator",
        "role": "Debate Facilitator",
        "description": "Guides the discussion, summarizes key points, and steers the conversation toward productive analysis.",
        "isJury": False,
    })
    
    return {"personas": personas}


@app.post("/tts")
async def synthesize_tts(request: TTSRequest):
    """Generate TTS audio for a persona and text."""
    try:
        persona_id = frontend_id_to_backend_id(request.persona_id)
        audio_bytes = await asyncio.to_thread(
            synthesize_speech,
            persona_id,
            request.text,
        )
    except Exception as exc:
        logger.exception("TTS failed for persona_id=%s", request.persona_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty text.")

    return Response(content=audio_bytes, media_type="audio/mpeg")


def main():
    """Main entry point for the API server."""
    import uvicorn
    
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("checker_of_facts.api:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    main()
