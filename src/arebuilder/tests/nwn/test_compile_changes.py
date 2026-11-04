"""Incremental compilation contracts shared by CI and local builds."""

import json
from pathlib import Path

import pytest

import arebuilder.nwn.compile as script_compile
from arebuilder.nwn.compile import CompileError, compile_scripts
from arebuilder.tests.nwn.test_compile import fake_compiler_factory


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("explicit_changes", [False, True])
def test_game_include_dependencies_follow_current_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    explicit_changes: bool,
) -> None:
    """Complete game includes must expose dependencies on overridden game resources."""

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "first.nss").write_text(
        '#include "base_bridge"\nvoid main() { int value = LeafValue(); }'
    )
    (scripts / "second.nss").write_text(
        '#include "inc_leaf"\nvoid main() { int value = LeafValue(); }'
    )
    (scripts / "unrelated.nss").write_text("void main() {}")
    sources = {
        "nwscript.nss": b"void PrintString(string text);",
        "base_bridge.nss": b'#include "base_middle"',
        "base_middle.nss": b'#include "inc_leaf"',
        "inc_leaf.nss": b"int LeafValue() { return 0; }",
    }
    game = tmp_path / "game"
    if archived:
        (game / "data").mkdir(parents=True)
        (game / "data" / "nwn_base.key").write_bytes(b"test archive")

        class FakeKeyReader:
            filenames = list(sources)

            def __init__(self, path: str) -> None:
                self.path = path

            def read_file(self, name: str) -> bytes:
                return sources[name]

            def close(self) -> None:
                pass

        monkeypatch.setattr(script_compile, "KeyReader", FakeKeyReader)
    else:
        (game / "ovr").mkdir(parents=True)
        for name, contents in sources.items():
            (game / "ovr" / name).write_bytes(contents)
    output = tmp_path / "output"
    state = tmp_path / "state.json"
    changes = tmp_path / "changes.txt"
    options = {
        "script_dir": scripts,
        "output_dir": output,
        "nwn_root": game,
        "state_path": state,
        "ci": True,
    }
    override = scripts / "base_bridge.nss"
    leaf = scripts / "inc_leaf.nss"
    # A local A* removes A -> B; deleting A* restores the shipped dependency.
    for bridge_override, expected in [
        (None, 2),
        ("int LeafValue() { return 9; }", 1),
        (None, 2),
    ]:
        if bridge_override is None:
            override.unlink(missing_ok=True)
        else:
            override.write_text(bridge_override)
        leaf.write_text("int LeafValue() { return 1; }")
        assert compile_scripts(**options, selector="all").compiled_count == 3
        previous = {
            name: (output / name).read_bytes()
            for name in ("first.ncs", "second.ncs", "unrelated.ncs")
        }
        leaf.write_text("int LeafValue() { return 2; }")
        changes.write_text("scripts/inc_leaf.nss:CHANGED:abc\n")

        result = compile_scripts(
            **options,
            changes_path=changes if explicit_changes else None,
        )

        assert result.compiled_count == expected
        assert ((output / "first.ncs").read_bytes() != previous["first.ncs"]) == (
            expected == 2
        )
        assert (output / "second.ncs").read_bytes() != previous["second.ncs"]
        assert (output / "unrelated.ncs").read_bytes() == previous["unrelated.ncs"]
        assert set(json.loads(state.read_text())["scripts"]) == {
            "first",
            "second",
            "unrelated",
            "inc_leaf",
            *({"base_bridge"} if bridge_override is not None else set()),
        }


def test_deleted_override_restores_game_entrypoint_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "alias.nss").write_text('#include "base_entry"')
    override = scripts / "base_entry.nss"
    override.write_text("int helper() { return 1; }")
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    (game / "ovr" / "base_entry.nss").write_text("void main() {}")
    output = tmp_path / "output"
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/base_entry.nss:CHANGED:abc\n")
    options = {
        "script_dir": scripts,
        "output_dir": output,
        "nwn_root": game,
        "changes_path": changes,
        "ci": True,
    }
    assert compile_scripts(**options).compiled_count == 0
    override.unlink()
    changes.write_text("scripts/base_entry.nss:REMOVED:def\n")
    capsys.readouterr()

    assert compile_scripts(**options).compiled_count == 1
    assert (output / "alias.ncs").read_bytes().startswith(b"NCS V1.0")
    assert capsys.readouterr().out == (
        "Compiling alias.nss\nCompilation complete.\n\n"
        "Stale build artifacts:\n- base_entry.ncs\n"
    )


def test_empty_override_shadows_game_source_during_native_compilation(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "base_bridge.nss").write_bytes(b"")
    (scripts / "first.nss").write_text(
        '#include "base_bridge"\nint Helper() { return 2; }\n'
        "void main() { int value = Helper(); }"
    )
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    (game / "ovr" / "base_bridge.nss").write_text("int Helper() { return 1; }")
    assert (
        compile_scripts(
            script_dir=scripts,
            output_dir=tmp_path / "output",
            nwn_root=game,
            use_state=False,
            ci=True,
        ).compiled_count
        == 1
    )


def test_changes_compile_transitive_dependents_without_touching_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "source-files"
    scripts.mkdir()
    (scripts / "leaf.nss").write_text("int value() { return 1; }")
    (scripts / "middle.nss").write_text(
        '#include "leaf"\nint other() { return value(); }'
    )
    (scripts / "first.nss").write_text('#include "middle"\nvoid main() {}')
    (scripts / "second.nss").write_text("void main() {}")
    changes = tmp_path / "changes.txt"
    changes.write_text(
        "source-files/leaf.nss:CHANGED:123\n"
        "source-files/leaf.nss:CHANGED:124\n"
        "elsewhere/second.nss:CHANGED:125\n"
        "source-files/subdir/second.nss:ADDED:125\n"
        "source-files/readme.md:CHANGED:125\n"
        "source-files/second.nss:NOT_CHANGED:125\n"
        "source-files:DIRECTORY_CHANGED:125\n"
    )
    state = tmp_path / "script_index.json"
    state.write_text("intentionally invalid state: must not be read")
    output = tmp_path / "output"
    output.mkdir()
    (output / "unrelated.ncs").write_bytes(b"keep")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    def unexpected_progress(*args: object, **kwargs: object) -> None:
        pytest.fail("CI compilation must not instantiate a progress bar")

    monkeypatch.setattr(script_compile, "ProgressBar", unexpected_progress)
    result = compile_scripts(
        script_dir=scripts,
        output_dir=output,
        changes_path=changes,
        state_path=state,
        ci=True,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert (output / "first.ncs").read_bytes() == b"compiled:first"
    assert {path.name for path in output.iterdir()} == {"first.ncs", "unrelated.ncs"}
    assert state.read_text() == "intentionally invalid state: must not be read"
    log = capsys.readouterr().out
    assert "Compiling first.nss\n" in log
    assert "Compilation complete.\n" in log
    assert "Stale build artifacts:\n- leaf.ncs\n" in log
    assert "change(s) found" not in log


@pytest.mark.parametrize("contents", ["", "\n", "other/ignored.txt:CHANGED:abc\n"])
def test_empty_changes_do_not_rebuild_missing_outputs(
    tmp_path: Path, contents: str
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unchanged.nss").write_text("void main() {}")
    changes = tmp_path / "changes.txt"
    changes.write_text(contents)

    result = compile_scripts(
        script_dir=scripts,
        output_dir=tmp_path / "output",
        changes_path=changes,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 0
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "record",
    [
        "scripts/first.nss",
        "scripts/first.nss:UNKNOWN:123",
        "scripts/first.nss:CHANGED:",
        "../scripts/first.nss:CHANGED:123",
        "/absolute/scripts/first.nss:CHANGED:123",
        "scripts/../scripts/first.nss:CHANGED:123",
    ],
)
def test_invalid_change_records_fail_before_outputs(
    tmp_path: Path, record: str
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    changes = tmp_path / "changes.txt"
    changes.write_text("\n" + record + "\n")
    with pytest.raises(CompileError, match="line 2"):
        compile_scripts(
            script_dir=scripts,
            output_dir=tmp_path / "output",
            changes_path=changes,
            compiler_factory=fake_compiler_factory,
        )
    assert not (tmp_path / "output").exists()


def test_changed_script_missing_from_checkout_is_an_error(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/missing.nss:CHANGED:123\n")
    with pytest.raises(CompileError, match="missing.nss"):
        compile_scripts(
            script_dir=scripts,
            output_dir=tmp_path / "output",
            changes_path=changes,
            compiler_factory=fake_compiler_factory,
        )


def test_deletion_only_changes_report_candidates_without_removing_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "restored.nss").write_text("void main() {}")
    changes = tmp_path / "changes.txt"
    changes.write_text(
        "scripts/z_deleted.nss:REMOVED:1\n"
        "scripts/a_deleted.nss:REMOVED:1\n"
        "scripts/a_deleted.nss:REMOVED:2\n"
        "scripts/restored.nss:REMOVED:1\n"
        "scripts/restored.nss:ADDED:2\n"
    )
    output = tmp_path / "output"
    output.mkdir()
    (output / "z_deleted.ncs").write_bytes(b"external cleanup")

    result = compile_scripts(
        script_dir=scripts,
        output_dir=output,
        changes_path=changes,
        ci=True,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert (output / "z_deleted.ncs").exists()
    assert (output / "restored.ncs").exists()
    assert capsys.readouterr().out.endswith(
        "Compilation complete.\n\nStale build artifacts:\n- a_deleted.ncs\n- z_deleted.ncs\n"
    )


@pytest.mark.parametrize("replacement", ["int helper() { return 1; }", ""])
def test_changes_report_lost_entrypoint_and_transitive_alias_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], replacement: str
) -> None:
    """Report obsolete executables even when the delta output has no previous NCS."""

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "entry.nss").write_text("void main() {}")
    (scripts / "alias.nss").write_text('#include "entry"')
    (scripts / "alias_outer.nss").write_text('#include "alias"')
    (scripts / "library.nss").write_text(
        '#include "entry"\nint another_helper() { return 2; }'
    )
    (scripts / "unaffected.nss").write_text("void main() {}")
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    baseline = tmp_path / "baseline"
    state = tmp_path / "state.json"
    assert (
        compile_scripts(
            script_dir=scripts,
            output_dir=baseline,
            nwn_root=game,
            state_path=state,
        ).compiled_count
        == 4
    )
    previous_outputs = {path.name: path.read_bytes() for path in baseline.glob("*.ncs")}
    assert set(previous_outputs) == {
        "entry.ncs",
        "alias.ncs",
        "alias_outer.ncs",
        "unaffected.ncs",
    }
    (scripts / "entry.nss").write_text(replacement)
    state.write_text("must not be read or changed")
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/entry.nss:CHANGED:abc\n")
    delta = tmp_path / "delta"
    capsys.readouterr()

    result = compile_scripts(
        script_dir=scripts,
        output_dir=delta,
        nwn_root=game,
        state_path=state,
        changes_path=changes,
        ci=True,
    )

    assert result.compiled_count == 0
    assert capsys.readouterr().out == (
        "Compilation complete.\n\nStale build artifacts:\n"
        "- alias.ncs\n- alias_outer.ncs\n- entry.ncs\n"
    )
    assert not delta.exists()
    assert state.read_text() == "must not be read or changed"
    assert {
        path.name: path.read_bytes() for path in baseline.glob("*.ncs")
    } == previous_outputs


@pytest.mark.parametrize("ci", [False, True])
def test_removed_sources_prune_state_and_live_output_without_sweeping_untracked_files(
    tmp_path: Path, ci: bool
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    output = tmp_path / "output"
    live = tmp_path / "live"
    state = tmp_path / "script_index.json"
    (scripts / "removed.nss").write_text("void main() {}")
    (scripts / "kept.nss").write_text("void main() {}")
    compile_scripts(
        script_dir=scripts,
        output_dir=output,
        secondary_output_dir=live,
        state_path=state,
        compiler_factory=fake_compiler_factory,
    )
    (scripts / "removed.nss").unlink()
    (output / "untracked.ncs").write_bytes(b"keep")

    result = compile_scripts(
        script_dir=scripts,
        output_dir=output,
        secondary_output_dir=live,
        state_path=state,
        ci=ci,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 0
    assert not (output / "removed.ncs").exists()
    assert not (live / "removed.ncs").exists()
    assert (output / "untracked.ncs").read_bytes() == b"keep"
    assert set(json.loads(state.read_text())["scripts"]) == {"kept"}


def test_stateful_cleanup_preserves_precompiled_fallback(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "removed.nss").write_text("void main() {}")
    output = tmp_path / "output"
    state = tmp_path / "script_index.json"
    options = {
        "script_dir": scripts,
        "output_dir": output,
        "state_path": state,
        "compiler_factory": fake_compiler_factory,
    }
    compile_scripts(**options)
    (scripts / "removed.nss").unlink()
    (output / "removed.ncs").write_bytes(b"precompiled fallback")

    compile_scripts(**options, preserved_outputs={"removed.ncs"})

    assert (output / "removed.ncs").read_bytes() == b"precompiled fallback"
    assert json.loads(state.read_text())["scripts"] == {}


@pytest.mark.parametrize("stateless", [False, True])
def test_failed_compile_does_not_clean_or_report_stale_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stateless: bool
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "removed.nss").write_text("void main() {}")
    output = tmp_path / "output"
    state = tmp_path / "script_index.json"
    compile_scripts(
        script_dir=scripts,
        output_dir=output,
        state_path=state,
        compiler_factory=fake_compiler_factory,
    )
    original_state = state.read_bytes()
    (scripts / "removed.nss").unlink()
    (scripts / "broken.nss").write_text("void main() {}")
    (output / "removed.ncs").write_bytes(b"keep on failure")
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/broken.nss:CHANGED:1\nscripts/removed.nss:REMOVED:1\n")

    class FailingCompiler:
        def compile(self, script_name: str) -> bytes:
            raise RuntimeError("compile failed")

    with pytest.raises(CompileError):
        compile_scripts(
            script_dir=scripts,
            output_dir=output,
            state_path=state,
            changes_path=changes if stateless else None,
            ci=True,
            compiler_factory=lambda resolver: FailingCompiler(),
        )

    assert (output / "removed.ncs").read_bytes() == b"keep on failure"
    assert state.read_bytes() == original_state
    log = capsys.readouterr().out
    assert "Compilation complete." not in log
    assert "Stale build artifacts:" not in log


@pytest.mark.parametrize("stateless", [False, True])
def test_removed_include_recompiles_surviving_dependents_with_native_compiler(
    tmp_path: Path, stateless: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "library.nss").write_text("int helper() { return 1; }")
    (scripts / "dependent.nss").write_text(
        '#include "library"\nvoid main() { int x = helper(); }'
    )
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    output = tmp_path / "output"
    state = tmp_path / "state.json"
    options = {
        "script_dir": scripts,
        "output_dir": output,
        "nwn_root": game,
        "state_path": state,
    }
    compile_scripts(**options)
    original_state = state.read_bytes()
    original_output = (output / "dependent.ncs").read_bytes()
    (scripts / "library.nss").unlink()
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/library.nss:REMOVED:abc\n")
    capsys.readouterr()

    with pytest.raises(CompileError):
        compile_scripts(**options, changes_path=changes if stateless else None, ci=True)

    assert state.read_bytes() == original_state
    assert (output / "dependent.ncs").read_bytes() == original_output
    log = capsys.readouterr().out
    assert "Compiling dependent.nss" in log
    assert "Compilation complete." not in log
    assert "Stale build artifacts:" not in log


@pytest.mark.parametrize("stateless", [False, True])
def test_removing_entire_script_directory_allows_deletion_only_run(
    tmp_path: Path, stateless: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "removed.nss").write_text("void main() {}")
    output = tmp_path / "output"
    state = tmp_path / "state.json"
    options = {
        "script_dir": scripts,
        "output_dir": output,
        "state_path": state,
        "compiler_factory": fake_compiler_factory,
        "ci": True,
    }
    compile_scripts(**options)
    (scripts / "removed.nss").unlink()
    scripts.rmdir()
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/removed.nss:REMOVED:abc\n")
    capsys.readouterr()

    assert (
        compile_scripts(
            **options, changes_path=changes if stateless else None
        ).compiled_count
        == 0
    )

    assert (output / "removed.ncs").exists() == stateless
    assert set(json.loads(state.read_text())["scripts"]) == (
        {"removed"} if stateless else set()
    )
    assert capsys.readouterr().out == (
        "Compilation complete.\n\nStale build artifacts:\n- removed.ncs\n"
    )


def test_ci_accepts_windows_separators_and_utf8_bom(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "example.nss").write_text("void main() {}")
    changes = tmp_path / "changes.txt"
    changes.write_bytes(b"\xef\xbb\xbfscripts\\example.nss:ADDED:<personal>\r\n")
    assert (
        compile_scripts(
            script_dir=scripts,
            output_dir=tmp_path / "output",
            changes_path=changes,
            compiler_factory=fake_compiler_factory,
        ).compiled_count
        == 1
    )


def test_changes_accepts_current_directory_as_script_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "sources"
    scripts.mkdir()
    (scripts / "example.nss").write_text("void main() {}")
    changes = tmp_path / "changes.txt"
    changes.write_text("sources/example.nss:ADDED:123\n")
    monkeypatch.chdir(scripts)
    assert (
        compile_scripts(
            script_dir=Path("."),
            output_dir=tmp_path / "output",
            changes_path=changes,
            compiler_factory=fake_compiler_factory,
        ).compiled_count
        == 1
    )


def test_failed_full_compile_preserves_previous_output_and_state(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "broken.nss").write_text("void main() { int x = ; }")
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    output = tmp_path / "output"
    output.mkdir()
    (output / "removed.ncs").write_bytes(b"keep on failure")
    state = tmp_path / "state.json"
    state.write_text('{"removed": 1}')

    with pytest.raises(CompileError):
        compile_scripts(
            script_dir=scripts,
            output_dir=output,
            state_path=state,
            selector="all",
            nwn_root=game,
            ci=True,
        )

    assert (output / "removed.ncs").read_bytes() == b"keep on failure"
    assert state.read_text() == '{"removed": 1}'


def test_full_compile_can_rebuild_invalid_state(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "example.nss").write_text("void main() {}")
    state = tmp_path / "state.json"
    state.write_text("invalid JSON")

    assert (
        compile_scripts(
            script_dir=scripts,
            output_dir=tmp_path / "output",
            state_path=state,
            selector="all",
            compiler_factory=fake_compiler_factory,
        ).compiled_count
        == 1
    )
    assert set(json.loads(state.read_text())["scripts"]) == {"example"}


def test_named_compile_preserves_removed_include_invalidation_for_next_run(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "library.nss").write_text("int helper() { return 1; }")
    (scripts / "dependent.nss").write_text(
        '#include "library"\nvoid main() { int x = helper(); }'
    )
    (scripts / "unrelated.nss").write_text("void main() {}")
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    state = tmp_path / "state.json"
    options = {
        "script_dir": scripts,
        "output_dir": tmp_path / "output",
        "nwn_root": game,
        "state_path": state,
    }
    compile_scripts(**options)
    (scripts / "library.nss").unlink()

    assert compile_scripts(**options, selector="unrelated").compiled_count == 1
    assert "library" in json.loads(state.read_text())["scripts"]
    with pytest.raises(CompileError):
        compile_scripts(**options)


def test_removed_primary_output_is_cleaned_even_when_shared_source_exists(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (scripts / "example.nss").write_text("void main() {}")
    (scripts / "kept.nss").write_text("void main() {}")
    (shared / "example.nss").write_text("int helper() { return 1; }")
    output = tmp_path / "output"
    state = tmp_path / "state.json"
    options = {
        "script_dir": scripts,
        "include_dirs": [shared],
        "output_dir": output,
        "state_path": state,
        "compiler_factory": fake_compiler_factory,
    }
    compile_scripts(**options)
    (scripts / "example.nss").unlink()

    assert compile_scripts(**options).compiled_count == 0
    assert not (output / "example.ncs").exists()
    assert "example" not in json.loads(state.read_text())["scripts"]


@pytest.mark.parametrize("fallback_has_entrypoint", [False, True])
def test_removed_primary_reports_stale_output_with_include_directory_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fallback_has_entrypoint: bool
) -> None:
    """Fallback includes can preserve aliases, but cannot regenerate primary output."""

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    primary = scripts / "example.nss"
    primary.write_text("void main() {}")
    (scripts / "alias.nss").write_text('#include "example"')
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "example.nss").write_text(
        "void main() { int value = 1; }"
        if fallback_has_entrypoint
        else "int helper() { return 1; }"
    )
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    output = tmp_path / "output"
    options = {
        "script_dir": scripts,
        "include_dirs": [shared],
        "output_dir": output,
        "nwn_root": game,
        "use_state": False,
        "ci": True,
    }
    assert compile_scripts(**options).compiled_count == 2
    previous = {path.name: path.read_bytes() for path in output.glob("*.ncs")}
    primary.unlink()
    changes = tmp_path / "changes.txt"
    changes.write_text("scripts/example.nss:REMOVED:abc\n")
    capsys.readouterr()

    result = compile_scripts(**options, changes_path=changes)

    assert result.compiled_count == int(fallback_has_entrypoint)
    assert (output / "example.ncs").read_bytes() == previous["example.ncs"]
    if fallback_has_entrypoint:
        assert (output / "alias.ncs").read_bytes() != previous["alias.ncs"]
        assert capsys.readouterr().out == (
            "Compiling alias.nss\nCompilation complete.\n\n"
            "Stale build artifacts:\n- example.ncs\n"
        )
    else:
        assert (output / "alias.ncs").read_bytes() == previous["alias.ncs"]
        assert capsys.readouterr().out == (
            "Compilation complete.\n\nStale build artifacts:\n"
            "- alias.ncs\n- example.ncs\n"
        )


def test_compiler_or_game_script_change_invalidates_saved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved hashes only describe outputs from the same compiler and game scripts."""

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "first.nss").write_text("void main() {}")
    (scripts / "second.nss").write_text("void main() {}")
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    options = {
        "script_dir": scripts,
        "output_dir": tmp_path / "output",
        "state_path": tmp_path / "state.json",
        "nwn_root": game,
        "compiler_factory": fake_compiler_factory,
    }
    assert compile_scripts(**options).compiled_count == 2
    assert compile_scripts(**options).compiled_count == 0

    (game / "ovr" / "nwscript.nss").write_text("void PrintInteger(int value);")
    assert compile_scripts(**options).compiled_count == 2
    assert compile_scripts(**options).compiled_count == 0

    monkeypatch.setattr(script_compile, "version", lambda _name: "0.0.0")
    assert compile_scripts(**options).compiled_count == 2


def test_interrupted_state_write_keeps_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state.json"
    script_compile.write_hash_index(state, {"first": 1}, "compiler")
    previous = state.read_bytes()

    def interrupted_replace(*_args: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(script_compile.os, "replace", interrupted_replace)
    with pytest.raises(CompileError):
        script_compile.write_hash_index(state, {"second": 2}, "compiler")

    assert state.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [state]
