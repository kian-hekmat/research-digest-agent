"""Every place that builds a Summarizer passes a spend ledger, so no code path
can call OpenAI without the call being recorded and budget-checked."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "evals").rglob("*.py")])
DEFINITION = ROOT / "app" / "services" / "summarize.py"


def _summarizer_calls_without_ledger(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "Summarizer" and not any(k.arg == "ledger" for k in node.keywords):
            missing.append(node.lineno)
    return missing


def test_sources_were_found():
    assert DEFINITION in SOURCES
    assert len(SOURCES) > 20


def test_every_summarizer_outside_its_module_passes_a_ledger():
    offenders = {
        str(path.relative_to(ROOT)): lines
        for path in SOURCES
        if path != DEFINITION and (lines := _summarizer_calls_without_ledger(path))
    }
    assert offenders == {}
