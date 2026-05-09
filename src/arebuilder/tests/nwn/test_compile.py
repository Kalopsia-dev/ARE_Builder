import json
import os
from pathlib import Path

import pytest

import arebuilder.nwn.compile as script_compile
from arebuilder.nwn.compile import CompileError, compile_scripts

FAKE_HOST_ROOT = "/host/project"


class FakeCompiler:
    """Small compiler backend that records deterministic outputs."""

    def __init__(self, resolver):
        """Initialize the instance with its required collaborators and state."""

        self.resolver = resolver

    def compile(self, script_name: str):
        """Compile a concrete script set in parallel, write outputs, and persist state hashes."""

        source = self.resolver(script_name)
        if source is None:
            raise AssertionError(f"Missing source for {script_name}")
        return f"compiled:{Path(script_name).stem}".encode("ascii")


def fake_compiler_factory(resolver):
    """Provide a fake implementation used to isolate this test case."""

    return FakeCompiler(resolver)


def test_default_compiler_reports_missing_nwscript_before_compiling(
    tmp_path: Path,
) -> None:
    """Verify that default compiler reports missing nwscript before compiling."""

    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    _write_script(script_dir / "main_one.nss", "void main() {}")

    with pytest.raises(CompileError) as exc_info:
        compile_scripts(
            script_dir=script_dir,
            output_dir=tmp_path / "compiled",
            selector="main_one",
            state_path=tmp_path / "temp" / "script_index.json",
            nwn_root=tmp_path / "empty-nwn-install",
        )

    assert "Unable to find nwscript.nss" in str(exc_info.value)
    assert "NWN_INSTALL_PATH" in str(exc_info.value)


def test_game_script_source_accepts_key_reader_filenames_property(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify key-file script discovery supports nwn 0.0.22's filenames property."""

    class FakeKeyReader:
        """Expose the nwn 0.0.22 filenames shape for a focused source test."""

        filenames = ["nwscript.nss", "x0_i0_match.nss", "readme.txt"]

        def __init__(self, path: str) -> None:
            self.path = path

        def read_file(self, script_name: str) -> bytes:
            return f"source:{script_name}".encode("ascii")

    monkeypatch.setattr(script_compile, "KeyReader", FakeKeyReader)
    key_path = tmp_path / "nwn_base.key"
    key_path.write_bytes(b"synthetic key placeholder")

    source = script_compile.GameScriptSource(key_path)

    assert source.scripts == {"nwscript.nss", "x0_i0_match.nss"}
    assert source.get("nwscript.nss") == b"source:nwscript.nss"


def test_modified_compile_initialises_state_then_noops_when_unchanged(
    tmp_path: Path,
) -> None:
    """Verify that modified compile initialises state then noops when unchanged."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    script_dir.mkdir()
    output_dir.mkdir()
    _write_script(script_dir / "inc_shared.nss", "int helper() { return 1; }")
    _write_script(
        script_dir / "main_one.nss",
        '#include "inc_shared"\nvoid main() { int x = helper(); }',
    )
    _write_script(script_dir / "main_two.nss", "void main() {}")

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 2
    assert {path.name for path in output_dir.glob("*.ncs")} == {
        "main_one.ncs",
        "main_two.ncs",
    }
    assert set(json.loads(state_path.read_text(encoding="utf-8"))) == {
        "inc_shared",
        "main_one",
        "main_two",
    }

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 0


def test_include_change_compiles_dependent_primary_scripts(tmp_path: Path) -> None:
    """Verify changing an include recompiles every dependent primary script."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    script_dir.mkdir()
    output_dir.mkdir()
    _write_script(script_dir / "inc_shared.nss", "int helper() { return 1; }")
    _write_script(
        script_dir / "main_one.nss",
        '#include "inc_shared"\nvoid main() { int x = helper(); }',
    )
    _write_script(script_dir / "main_two.nss", "void main() {}")

    compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )
    (output_dir / "main_one.ncs").unlink()
    _write_script(script_dir / "inc_shared.nss", "int helper() { return 2; }")

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert (output_dir / "main_one.ncs").read_bytes() == b"compiled:main_one"


def test_transitive_include_change_compiles_top_level_dependents(
    tmp_path: Path,
) -> None:
    """Verify changing a nested include recompiles top-level dependent scripts."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    script_dir.mkdir()
    output_dir.mkdir()
    _write_script(script_dir / "inc_leaf.nss", "int leaf() { return 1; }")
    _write_script(
        script_dir / "inc_mid.nss",
        '#include "inc_leaf"\nint mid() { return leaf(); }',
    )
    _write_script(
        script_dir / "main_one.nss",
        '#include "inc_mid"\nvoid main() { int x = mid(); }',
    )
    _write_script(script_dir / "main_two.nss", "void main() {}")

    compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )
    (output_dir / "main_one.ncs").unlink()
    _write_script(script_dir / "inc_leaf.nss", "int leaf() { return 2; }")

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert (output_dir / "main_one.ncs").read_bytes() == b"compiled:main_one"


def test_selector_modes_compile_expected_scripts(tmp_path: Path) -> None:
    """Verify named, wildcard, and all selectors compile the intended script sets."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    script_dir.mkdir()
    output_dir.mkdir()
    _write_script(script_dir / "inc_shared.nss", "int helper() { return 1; }")
    _write_script(
        script_dir / "main_one.nss",
        '#include "inc_shared"\nvoid main() { int x = helper(); }',
    )
    _write_script(script_dir / "main_two.nss", "void main() {}")

    wildcard_result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="main_*",
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )
    assert wildcard_result.compiled_count == 2

    include_result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="inc_shared",
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )
    assert include_result.compiled_count == 1
    assert (output_dir / "main_one.ncs").read_bytes() == b"compiled:main_one"

    explicit_result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="main_two",
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )
    assert explicit_result.compiled_count == 1
    assert (output_dir / "main_two.ncs").exists()


def test_compile_all_clears_only_output_root_ncs_files(tmp_path: Path) -> None:
    """Verify full compiles remove only compiled NCS files from output roots."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    nested_dir = output_dir / "target"
    script_dir.mkdir()
    nested_dir.mkdir(parents=True)
    _write_script(script_dir / "main_one.nss", "void main() {}")
    (output_dir / "stale.ncs").write_bytes(b"stale")
    (nested_dir / "target_script.ncs").write_bytes(b"target")

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="all",
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert not (output_dir / "stale.ncs").exists()
    assert (output_dir / "main_one.ncs").exists()
    assert (nested_dir / "target_script.ncs").exists()


def test_target_compile_uses_shared_includes_but_writes_only_primary_outputs(
    tmp_path: Path,
) -> None:
    """
    Verify that target compile uses shared includes but writes only primary outputs.
    """

    script_dir = tmp_path / "target" / "scripts"
    shared_dir = tmp_path / "are-resources" / "scripts"
    output_dir = tmp_path / "compiled" / "target"
    script_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    _write_script(shared_dir / "inc_shared.nss", "int helper() { return 1; }")
    _write_script(
        script_dir / "target_main.nss",
        '#include "inc_shared"\nvoid main() { int x = helper(); }',
    )

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="all",
        include_dirs=[shared_dir],
        use_state=False,
        clear_output_on_all=False,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert {path.name for path in output_dir.glob("*.ncs")} == {"target_main.ncs"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="Creating symlinks on Windows can require elevated privileges.",
)
def test_compile_remaps_containerized_host_absolute_script_symlink_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Dockerized compiles can read host-absolute links under the mounted root."""

    mounted_root = tmp_path / "mounted"
    script_dir = mounted_root / "are-resources" / "scripts"
    target_dir = mounted_root / "other-project" / "src"
    output_dir = mounted_root / "compiled-resources"
    script_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    output_dir.mkdir()
    _write_script(target_dir / "test_script.nss", "void main() {}")
    (script_dir / "test_script.nss").symlink_to(
        f"{FAKE_HOST_ROOT}/other-project/src/test_script.nss"
    )
    monkeypatch.setenv("AREDEV_HOST_ROOT", FAKE_HOST_ROOT)
    monkeypatch.setenv("AREDEV_CONFIG_ROOT", str(mounted_root))

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="test_script",
        state_path=mounted_root / "temp" / "script_index.json",
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert (output_dir / "test_script.ncs").read_bytes() == (b"compiled:test_script")


def test_compile_all_does_not_compile_include_that_inherits_main(
    tmp_path: Path,
) -> None:
    """Verify that compile all does not compile include that inherits main."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    script_dir.mkdir()
    output_dir.mkdir()
    _write_script(script_dir / "entry_script.nss", "void main() {}")
    _write_script(
        script_dir / "inc_wrapper.nss",
        '#include "entry_script"\nint helper() { return 1; }',
    )

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="all",
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert {path.name for path in output_dir.glob("*.ncs")} == {"entry_script.ncs"}


def test_unterminated_block_comment_does_not_create_entry_point(
    tmp_path: Path,
) -> None:
    """Verify that unterminated block comment does not create entry point."""

    script_dir = tmp_path / "scripts"
    output_dir = tmp_path / "compiled"
    state_path = tmp_path / "temp" / "script_index.json"
    script_dir.mkdir()
    output_dir.mkdir()
    _write_script(script_dir / "actual_main.nss", "void main() {}")
    _write_script(
        script_dir / "inc_bodytailor.nss",
        "int helper() { return 1; }\n\n/*\nvoid main(){}",
    )

    result = compile_scripts(
        script_dir=script_dir,
        output_dir=output_dir,
        selector="all",
        state_path=state_path,
        compiler_factory=fake_compiler_factory,
    )

    assert result.compiled_count == 1
    assert {path.name for path in output_dir.glob("*.ncs")} == {"actual_main.ncs"}


def test_script_index_reports_include_cycles(tmp_path: Path) -> None:
    """Verify cyclic includes fail with the scripts that form the cycle."""

    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    _write_script(
        script_dir / "inc_alpha.nss",
        '#include "inc_beta"\nint a() { return 1; }',
    )
    _write_script(
        script_dir / "inc_beta.nss",
        '#include "inc_alpha"\nint b() { return 1; }',
    )

    with pytest.raises(CompileError) as exc_info:
        script_compile.ScriptIndex(script_dir)

    message = str(exc_info.value)
    assert message.startswith("Error: Include cycle detected: ")
    cycle_path = message.removeprefix("Error: Include cycle detected: ").split(" -> ")
    assert cycle_path[0] == cycle_path[-1]
    assert set(cycle_path) == {"inc_alpha.nss", "inc_beta.nss"}


def test_primary_script_directory_wins_include_directory_name_collisions(
    tmp_path: Path,
) -> None:
    """Verify primary scripts shadow include directories when names collide."""

    script_dir = tmp_path / "scripts"
    shared_dir = tmp_path / "shared"
    script_dir.mkdir()
    shared_dir.mkdir()
    _write_script(
        script_dir / "main_one.nss",
        '#include "inc_shared"\nvoid main() { int x = helper(); }',
    )
    _write_script(script_dir / "inc_shared.nss", "int helper() { return 1; }")
    _write_script(shared_dir / "inc_shared.nss", "int helper() { return 2; }")

    script_index = script_compile.ScriptIndex(script_dir, include_dirs=[shared_dir])
    include = script_index.get("inc_shared")

    assert include is not None
    assert include.path == script_dir / "inc_shared.nss"
    assert include.contents == b"int helper() { return 1; }\n"


def _write_script(path: Path, text: str) -> None:
    """Write a small NWScript source file used by compiler tests."""

    path.write_text(text + "\n", encoding="latin-1")
