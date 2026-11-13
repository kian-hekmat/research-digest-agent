"""Every place that builds a Summarizer or an Embedder passes a spend ledger,
so no code path can call OpenAI without the call being recorded (and, for
chat calls, budget-checked)."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "evals").rglob("*.py")])
DEFINITIONS = {
    "Summarizer": ROOT / "app" / "services" / "summarize.py",
    "Embedder": ROOT / "app" / "services" / "embed.py",
}


def _calls_without_ledger(path: Path, cls: str) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == cls and not any(k.arg == "ledger" for k in node.keywords):
            missing.append(node.lineno)
    return missing


def test_sources_were_found():
    assert set(DEFINITIONS.values()) <= set(SOURCES)
    assert len(SOURCES) > 20


def test_every_summarizer_and_embedder_outside_its_module_passes_a_ledger():
    offenders = {
        f"{path.relative_to(ROOT)} ({cls})": lines
        for cls, definition in DEFINITIONS.items()
        for path in SOURCES
        if path != definition and (lines := _calls_without_ledger(path, cls))
    }
    assert offenders == {}
