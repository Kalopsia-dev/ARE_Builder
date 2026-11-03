"""Application exports, loaded only when their build functionality is requested."""

__all__ = [
    "BuildEngine",
    "ModuleBuildWorkspace",
    "scan_included_files",
]


def __getattr__(name: str):
    if name in __all__:
        from arebuilder.app.arebuilder import engine

        return getattr(engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
