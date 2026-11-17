"""A simulated operations environment: batch jobs, their runs, logs and S3 buckets.

Everything here is read-only. Every query validates its arguments, because the
caller is a language model whose inputs may have been steered by a poisoned log.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MAX_LOG_LINES = 50
MAX_RUNS = 10
MAX_OBJECTS = 100
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


class QueryError(ValueError):
    """A rejected query. The message is safe to show to the model."""


class World:
    def __init__(self, data: dict):
        self.jobs: dict[str, dict] = data["jobs"]
        self.runs: dict[str, list[dict]] = data.get("runs", {})
        self.logs: dict[str, list[dict]] = data.get("logs", {})
        self.s3: dict[str, list[dict]] = data.get("s3", {})

    @classmethod
    def from_file(cls, path: str | Path) -> "World":
        return cls(json.loads(Path(path).read_text(encoding="utf-8"))["world"])

    # --- validation helpers ---------------------------------------------------

    def _job(self, job: str) -> str:
        if not isinstance(job, str) or not _NAME.match(job) or job not in self.jobs:
            raise QueryError(f"unknown job. Known jobs: {sorted(self.jobs)}")
        return job

    @staticmethod
    def _limit(limit: int, cap: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise QueryError("limit must be a positive integer")
        return min(limit, cap)

    # --- queries --------------------------------------------------------------

    def list_jobs(self) -> dict:
        return {"jobs": [{"name": n, "schedule": j.get("schedule", "")} for n, j in sorted(self.jobs.items())]}

    def get_job_config(self, job: str) -> dict:
        return {"job": self._job(job), "config": self.jobs[job]}

    def get_recent_runs(self, job: str, limit: int = 5) -> dict:
        job, limit = self._job(job), self._limit(limit, MAX_RUNS)
        return {"job": job, "runs": self.runs.get(job, [])[:limit]}

    def search_logs(self, job: str, contains: str = "", level: str = "", limit: int = 20) -> dict:
        job, limit = self._job(job), self._limit(limit, MAX_LOG_LINES)
        if level and level not in LEVELS:
            raise QueryError(f"level must be one of {LEVELS} or empty")
        if not isinstance(contains, str) or len(contains) > 100:
            raise QueryError("contains must be text of at most 100 characters")
        needle = contains.lower()
        lines = [
            line for line in self.logs.get(job, [])
            if (not level or line["level"] == level) and (not needle or needle in line["msg"].lower())
        ]
        # Most recent last, like a real log tail.
        return {"job": job, "total_matches": len(lines), "lines": lines[-limit:]}

    def list_s3_objects(self, bucket: str, prefix: str = "", limit: int = 50) -> dict:
        limit = self._limit(limit, MAX_OBJECTS)
        if not isinstance(bucket, str) or not _NAME.match(bucket):
            raise QueryError("invalid bucket name")
        if not isinstance(prefix, str) or len(prefix) > 200 or ".." in prefix or prefix.startswith("/"):
            raise QueryError("invalid prefix")
        if bucket not in self.s3:
            return {"bucket": bucket, "exists": False, "objects": []}
        objects = [o for o in self.s3[bucket] if o["key"].startswith(prefix)]
        return {"bucket": bucket, "exists": True, "total_matches": len(objects), "objects": objects[:limit]}

    # --- scoping --------------------------------------------------------------

    def scope_for(self, job: str) -> tuple[set[str], set[str]]:
        """Jobs and buckets an investigation of `job` may touch: itself and its
        declared upstream jobs, plus the buckets those configs name."""
        job = self._job(job)
        jobs = {job, *[u for u in self.jobs[job].get("upstream", []) if u in self.jobs]}
        buckets: set[str] = set()
        for name in jobs:
            for side in ("input", "output"):
                bucket = self.jobs[name].get(side, {}).get("bucket")
                if bucket:
                    buckets.add(bucket)
        return jobs, buckets
