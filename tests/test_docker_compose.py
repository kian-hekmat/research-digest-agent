"""Regression test for the missed-digest incident (2026-09-21).

The `temporal` dev server keeps all state - including Schedules and their
next-fire bookkeeping - in memory unless started with --db-filename pointed
at a persistent volume. Without it, every container restart (a laptop
sleeping, colima stopping, `docker-compose down`) silently wipes the weekly
email / daily digest schedules and resets their clock to "now" - any fire
time that should have happened while the container was down is lost with no
error and no record. The worker recreating the schedules idempotently on its
next startup only avoids a duplicate-schedule error; it does not recover what
was missed. This is a plain-text check (no docker-compose.yml parser
dependency) that the persistence flag and its backing volume stay wired up.
"""
import re
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def _service_block(text: str, service: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{service}:")
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start + 1) if line and not line[0].isspace()),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_temporal_service_persists_state_via_db_filename():
    block = _service_block(COMPOSE_PATH.read_text(), "temporal")
    # Drop comment lines first - the explanatory comment above the command
    # itself mentions "--db-filename" in prose, which would otherwise match.
    code_only = "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))

    match = re.search(r"--db-filename\s+(\S+)", code_only)
    assert match, "temporal service must pass --db-filename, or Schedules are lost on every restart"

    db_path = match.group(1)
    volume_match = re.search(r"temporal_data:(/\S+)", code_only)
    assert volume_match, "temporal's --db-filename target must be backed by a mounted volume"

    mount_target = volume_match.group(1)
    assert db_path.startswith(mount_target + "/"), (
        f"--db-filename ({db_path}) must live under the temporal_data mount ({mount_target}), "
        "otherwise it still writes to the container's ephemeral filesystem"
    )


def test_worker_receives_the_summarization_settings():
    """The worker is what runs summarize_paper. Settings it isn't passed fall
    back to their defaults silently - summaries just stay off, or it looks for
    Ollama on the container's own localhost, where nothing is listening."""
    block = _service_block(COMPOSE_PATH.read_text(), "worker")
    code_only = "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))

    assert re.search(r"SUMMARY_BACKEND: \$\{SUMMARY_BACKEND", code_only)
    assert re.search(r"OLLAMA_MODEL: \$\{OLLAMA_MODEL", code_only)
    base_url = re.search(r"OLLAMA_BASE_URL: \$\{OLLAMA_BASE_URL:-(\S+)\}", code_only)
    assert base_url, "worker must be given OLLAMA_BASE_URL"
    assert "host.docker.internal" in base_url.group(1), (
        "inside a container, localhost is the container itself - the default "
        "must point at the Mac, where Ollama runs"
    )


def test_temporal_data_volume_is_declared_top_level():
    text = COMPOSE_PATH.read_text()
    top_level_volumes = re.search(r"(?m)^volumes:\n((?:^ {2}\S.*\n?)+)", text)
    assert top_level_volumes, "no top-level `volumes:` block found in docker-compose.yml"
    assert "temporal_data" in top_level_volumes.group(1), (
        "temporal_data must be declared as a top-level named volume, or docker-compose "
        "creates it anonymously and it won't be reused across `up`/`down` cycles"
    )
