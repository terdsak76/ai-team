"""Muse orchestration primitives for the AI development team."""

__all__ = ["MuseOrchestrator", "MuseRunResult"]


def __getattr__(name):
    """Load orchestration exports without coupling schema imports to agents."""
    if name in __all__:
        from muse.orchestrator import MuseOrchestrator, MuseRunResult

        return {"MuseOrchestrator": MuseOrchestrator, "MuseRunResult": MuseRunResult}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
