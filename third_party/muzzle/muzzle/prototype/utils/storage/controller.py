"""MongoDB-backed Storage Controller
-----------------------------------

This module provides a StorageController class that persists artifacts in 
MongoDB via PyMongo.

Behavioral parity with previous filesystem approach (now simplified for DB):
- Each run has a run_id timestamp string ("%Y-%m-%d_%H:%M:%S").
- Artifacts no longer carry filesystem filenames; uniqueness & referencing
    provided by an internal artifact_id and their association to run_id + type.
- Optional goal_idx field restored for artifacts tied to a specific adversarial
    objective (e.g., adversarial playbooks, prompts, prompt injections, dispatched
    tasks, transcripts). For benign artifacts it is None.
- Artifact buckets retained: transcripts, playbooks, grafts, prompts,
    prompt_injections, dispatched_tasks.

Schema:
Runs collection (name: <prefix>runs):
- run_id: str (PK, timestamp format "%Y-%m-%d_%H:%M:%S")
- task_spec: dict (original task specification)
- created_at: datetime
- updated_at: datetime

Artifact collections (per type): transcripts, playbooks, grafts, prompts,
prompt_injections, dispatched_tasks (optionally prefixed):
- artifact_id: str (unique identifier; UUID-like)
- run_id: str (FK reference to runs.run_id)
- goal_idx: Optional[int] (adversarial goal index if applicable)
- data: dict (payload)
- created_at: datetime
- updated_at: datetime

Indexing:
- runs: unique on (run_id)
- each artifact collection:
    * unique on (artifact_id)
    * index on (run_id)
    * index on (run_id, created_at) for latest queries
    * index on (goal_idx) for goal-scoped queries

Configuration via environment variables:
- MONGODB_URI                   (default: mongodb://localhost:27017)
- MONGODB_DB_NAME               (default: muzzle)
- MONGODB_COLLECTION_PREFIX     (default: "")  # e.g., "rtm_" -> rtm_transcripts / rtm_runs
"""

from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import asdict

class StorageController:
    """MongoDB-backed storage controller for runs and artifacts.

    Notes:
    - Filenames removed; callers receive artifact_id after storing.
    - Retrieval by artifact_id or listing by (run_id, artifact_type).
    """

    # Mapping of logical artifact buckets used by agents
    ARTIFACT_TYPES = {
        "transcripts",
        "playbooks",
        "grafts",
        "prompts",
        "prompt_injections",
        "dispatched_tasks",
        "judge_evaluations",  # evaluation artifacts produced by JudgeAgent
    }

    def __init__(
        self,
        uri: Optional[str] = None,
        db_name: Optional[str] = None,
        collection_prefix: Optional[str] = None,
        connect: bool = True,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self._uri = uri or os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        self._db_name = db_name or os.getenv("MONGODB_DB_NAME", "muzzle")
        # Optional collection prefix to namespace per-type collections
        self._collection_prefix = (
            collection_prefix if collection_prefix is not None else os.getenv("MONGODB_COLLECTION_PREFIX", "")
        )

        self._client: Optional[Any] = None  # AsyncMongoClient at runtime
        # Cache of per-type collections:
        self._collections: Dict[str, Any] = {}
        # Track which collections have had indexes ensured
        self._indexed: set[str] = set()

        if connect:
            self._ensure_connection()

    # -------------------------
    # Store API
    # -------------------------
    async def store_dispatched_task(self, task: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int] = None) -> Optional[str]:
        """Store a dispatched task; return artifact_id or None if skipped."""
        return await self._conditional_store("dispatched_tasks", task, run_id, goal_idx)

    async def store_graft(self, graft: Dict[str, Any], run_id: Optional[str]) -> Optional[str]:
        """Store a graft; return artifact_id or None if skipped."""
        return await self._conditional_store("grafts", graft, run_id, None)

    async def store_prompt(self, prompt: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int] = None) -> Optional[str]:
        """Store a prompt; return artifact_id or None if skipped."""
        return await self._conditional_store("prompts", prompt, run_id, goal_idx)

    async def store_prompt_injection(self, prompt_injection: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int] = None) -> Optional[str]:
        """Store a prompt injection; return artifact_id or None if skipped."""
        return await self._conditional_store("prompt_injections", prompt_injection, run_id, goal_idx)

    async def store_playbook(self, playbook: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int] = None) -> Optional[str]:
        """Store a playbook; return artifact_id or None if skipped."""
        return await self._conditional_store("playbooks", playbook, run_id, goal_idx)

    async def store_transcript(self, transcript: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int] = None) -> Optional[str]:
        """Store a transcript; return artifact_id or None if skipped."""
        return await self._conditional_store("transcripts", transcript, run_id, goal_idx)

    async def store_judge_evaluation(self, evaluation: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int] = None) -> Optional[str]:
        """Store a judge evaluation artifact; return artifact_id or None if skipped."""
        return await self._conditional_store("judge_evaluations", evaluation, run_id, goal_idx)

    # -------------------------
    # Runs API
    # -------------------------
    async def create_run(self, task_spec: Dict[str, Any], run_id: Optional[str] = None) -> str:
        """Create (or upsert) a run document and return its run_id.

        If run_id is None, generate a timestamp-based ID. If provided, validate
        format. Upsert semantics: existing run_id updates task_spec & updated_at,
        retains original created_at.
        """
        actual_run_id = run_id or self._generate_timestamped_id()
        self._validate_timestamped_id(actual_run_id)
        col = await self._get_runs_collection()
        now = datetime.now(timezone.utc)

        # Check if task spec is a dataclass instance and convert to dict
        if hasattr(task_spec, "__dataclass_fields__"):
            task_spec = asdict(task_spec)

        await col.update_one(
            {"run_id": actual_run_id},
            {
                "$set": {"task_spec": task_spec, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return actual_run_id

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a run document by run_id."""
        self._validate_timestamped_id(run_id)
        col = await self._get_runs_collection()
        return await col.find_one({"run_id": run_id}, projection={"_id": False})

    async def list_run_ids(self) -> List[str]:
        """List all run_ids from runs collection."""
        col = await self._get_runs_collection()
        cursor = col.find({}, projection={"_id": False, "run_id": True}).sort("created_at", -1)
        docs = await cursor.to_list(length=None)
        return [d.get("run_id", "") for d in docs if isinstance(d.get("run_id"), str)]

    # ------------------------
    # Read API
    # ------------------------
    async def get_artifact(self, artifact_id: str, artifact_type: str) -> Optional[Dict[str, Any]]:
        """Return stored artifact by its artifact_id and type."""
        self._validate_type(artifact_type)
        col = await self._get_collection(artifact_type)
        return await col.find_one({"artifact_id": artifact_id}, projection={"_id": False})

    async def list_artifacts(self, run_id: str, artifact_type: str) -> List[Dict[str, Any]]:
        """List artifacts of a given type for a run_id ordered by creation time."""
        self._validate_timestamped_id(run_id)
        self._validate_type(artifact_type)
        col = await self._get_collection(artifact_type)
        cursor = col.find(
            {"run_id": run_id},
            projection={"_id": False},
        ).sort("created_at", 1)
        return await cursor.to_list(length=None)

    async def list_runs(self) -> List[str]:
        """List run_ids (new representation replacing legacy run_path)."""
        return await self.list_run_ids()

    async def get_latest(self, run_id: str, artifact_type: str) -> Optional[Dict[str, Any]]:
        """Return the most recently created artifact for run_id & type."""
        self._validate_timestamped_id(run_id)
        self._validate_type(artifact_type)
        col = await self._get_collection(artifact_type)
        cursor = col.find({"run_id": run_id}, projection={"_id": False}).sort(
            "created_at", -1
        ).limit(1)
        docs = await cursor.to_list(length=1)
        return docs[0] if docs else None

    # -------------------------
    # Internal helpers
    # -------------------------
    async def _conditional_store(self, artifact_type: str, data: Dict[str, Any], run_id: Optional[str], goal_idx: Optional[int]) -> Optional[str]:
        """Store only if run_id provided; return artifact_id or None."""
        if run_id is None:
            self.logger.debug("Skipping store (run_id is None): type=%s", artifact_type)
            return None
        self._validate_type(artifact_type)
        self._validate_timestamped_id(run_id)
        try:
            return await self._store_artifact(artifact_type, data, run_id, goal_idx)
        except Exception as e:  # pragma: no cover
            self.logger.error("Failed to store artifact: %s", e)
            raise

    async def _store_artifact(self, artifact_type: str, data: Dict[str, Any], run_id: str, goal_idx: Optional[int]) -> str:
        col = await self._get_collection(artifact_type)
        now = datetime.now(timezone.utc)
        artifact_id = self._generate_timestamped_id()

        # Check if data is a dataclass instance and convert to dict
        if hasattr(data, "__dataclass_fields__"):
            data = asdict(data)

        await col.insert_one(
            {
                "artifact_id": artifact_id,
                "run_id": run_id,
                "data": data,
                "goal_idx": goal_idx,
                "created_at": now,
                "updated_at": now,
            }
        )
        self.logger.info(
            f"Stored artifact: type={artifact_type}, for run_id={run_id} and goal_idx={goal_idx}"
        )
        return artifact_id

    def _ensure_connection(self) -> Any:
        if self._client is not None:
            return self._client

        # Lazy connect client only
        try:
            from pymongo import AsyncMongoClient  # type: ignore
        except Exception as e:  # pragma: no cover - runtime import error
            raise ImportError(
                "PyMongo async client is required. Install with `pip install pymongo>=4.6`."
            ) from e

        self._client = AsyncMongoClient(self._uri)
        return self._client

    async def _get_collection(self, artifact_type: str) -> Any:
        """Return a per-type collection, creating and indexing on first access."""
        self._validate_type(artifact_type)
        self._ensure_connection()
        if artifact_type in self._collections:
            return self._collections[artifact_type]

        db = self._client[self._db_name]  # type: ignore[index]
        col_name = f"{self._collection_prefix}{artifact_type}"
        col = db[col_name]
        if col_name not in self._indexed:
            await self._ensure_indexes(col)
            self._indexed.add(col_name)
        self._collections[artifact_type] = col
        return col

    @staticmethod
    async def _ensure_indexes(col: Any) -> None:
        await col.create_index([("artifact_id", 1)], unique=True, name="uniq_artifact_id")
        await col.create_index([("run_id", 1)], name="by_run_id")
        await col.create_index([("run_id", 1), ("created_at", -1)], name="by_run_created")
        await col.create_index([("goal_idx", 1)], name="by_goal_idx")

    async def _get_runs_collection(self) -> Any:
        """Return the runs collection (create & index if missing)."""
        self._ensure_connection()
        key = "__runs__"
        if key in self._collections:
            return self._collections[key]
        db = self._client[self._db_name]  # type: ignore[index]
        col_name = f"{self._collection_prefix}runs"
        col = db[col_name]
        # Runs collection indexes
        if col_name not in self._indexed:
            await col.create_index([("run_id", 1)], unique=True, name="uniq_run_id")
            await col.create_index([("created_at", -1)], name="runs_created")
            self._indexed.add(col_name)
        self._collections[key] = col
        return col

    @staticmethod
    def _generate_timestamped_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S")

    @staticmethod
    def _validate_timestamped_id(id: str) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}", id):
            raise ValueError(
                f"id '{id}' is not in required format YYYY-MM-DD_HH:MM:SS"
            )

    @staticmethod
    def _validate_type(artifact_type: str) -> None:
        if artifact_type not in StorageController.ARTIFACT_TYPES:
            raise ValueError(
                f"Unknown artifact_type '{artifact_type}'. Expected one of: {sorted(StorageController.ARTIFACT_TYPES)}"
            )

    # -------------------------
    # Lifecycle
    # -------------------------
    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # pragma: no cover - best-effort close
                pass
            finally:
                self._client = None
                self._collections.clear()

    async def wipe(self) -> None:
        """Wipe all data from the database (for testing/demo purposes)."""
        self._ensure_connection()
        db = self._client[self._db_name]  # type: ignore[index]
        for artifact_type in StorageController.ARTIFACT_TYPES:
            col_name = f"{self._collection_prefix}{artifact_type}"
            await db[col_name].drop()
        runs_col_name = f"{self._collection_prefix}runs"
        await db[runs_col_name].drop()
        self._collections.clear()


__all__ = ["StorageController"]

if __name__ == "__main__":
    # Simple async demo
    import asyncio

    async def _demo() -> None:
        logging.basicConfig(level=logging.INFO)
        sc = StorageController()
        await sc.wipe()
        await sc.close()

    asyncio.run(_demo())