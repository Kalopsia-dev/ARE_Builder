from pathlib import Path

import pytest

from arebuilder.app.cli import main
from arebuilder.builder.archive import read_erf_members
from arebuilder.nwn.compat import read_gff
from arebuilder.nwn.compile import CompileResult
from arebuilder.tests.fixtures import create_synthetic_fixture


def _assert_resource_link(
    path: Path,
    *,
    symlink_target: str,
    source_path: Path,
) -> None:
    """Assert a generated override resource was linked for the current platform."""

    if path.is_symlink():
        assert path.readlink().as_posix() == symlink_target
        return
    assert path.exists()
    assert path.is_file()
    assert path.read_bytes() == source_path.read_bytes()


def test_end_to_end_build_and_link(tmp_path: Path, monkeypatch) -> None:
    """Verify the full CLI build/link workflow produces expected archives and override links."""

    compile_calls = _install_fake_script_compiler(monkeypatch)
    fixture = create_synthetic_fixture(tmp_path / "fixture")

    assert _run_builder(fixture, "all", fixture.module_name) == 0

    expected_files = {
        "module.ifo",
        "creaturepalcus.itp",
        "doorpalcus.itp",
        "encounterpalcus.itp",
        "itempalcus.itp",
        "placeablepalcus.itp",
        "soundpalcus.itp",
        "storepalcus.itp",
        "triggerpalcus.itp",
        "waypointpalcus.itp",
        "fixture_precompiled.ncs",
        "ignored.ncs",
    }
    assert {path.name for path in fixture.build_dir.iterdir()} == expected_files
    assert compile_calls[0]["script_dir"] == fixture.module_resources_dir / "scripts"
    assert compile_calls[0]["include_dirs"] == [fixture.are_resources_dir / "scripts"]
    assert compile_calls[0]["selector"] is None
    assert compile_calls[0]["state_path"] == (
        fixture.root / "temp" / "script_index_synthetic.json"
    )
    assert compile_calls[0]["use_state"] is True

    archive_members = read_erf_members(fixture.module_archive_path)
    assert sorted(archive_members) == [
        "creaturepalcus.itp",
        "doorpalcus.itp",
        "encounterpalcus.itp",
        "itempalcus.itp",
        "module.ifo",
        "placeablepalcus.itp",
        "soundpalcus.itp",
        "storepalcus.itp",
        "triggerpalcus.itp",
        "waypointpalcus.itp",
    ]

    module_root, _ = read_gff(fixture.build_dir / "module.ifo")
    assert module_root["Mod_Name"].entries
    assert str(module_root["Mod_Entry_Area"]) == "module_entry"
    assert float(module_root["Mod_Entry_X"]) == 10.0
    assert float(module_root["Mod_Entry_Y"]) == 10.0
    assert int(module_root["Mod_StartYear"]) == 100
    assert str(module_root["Mod_CustomTlk"]) == "fixture"
    assert [str(entry["Mod_Hak"]) for entry in module_root["Mod_HakList"]] == [
        "fixture_hak"
    ]

    assert (fixture.tlk_dir / "fixture.tlk").exists()
    assert (fixture.hak_dir / "fixture_hak.hak").exists()

    assert (fixture.override_dir / "module.ifo").exists() is False
    _assert_resource_link(
        fixture.override_dir / "global_script.ncs",
        symlink_target="/var/builder/compiled-resources/global_script.ncs",
        source_path=fixture.compiled_resources_dir / "global_script.ncs",
    )

    assert _run_builder(fixture, "link", fixture.module_name) == 0
    _assert_resource_link(
        fixture.override_dir / "module.ifo",
        symlink_target=(
            f"/var/builder/compiled-resources/{fixture.module_name}/module.ifo"
        ),
        source_path=fixture.build_dir / "module.ifo",
    )
    _assert_resource_link(
        fixture.override_dir / "beast.utc",
        symlink_target=(
            "/var/builder/synthetic-resources/creature/Creatures/Beasts/beast.utc"
        ),
        source_path=next(fixture.module_resources_dir.rglob("beast.utc")),
    )


def test_native_link_uses_project_target_resource_dir(tmp_path: Path) -> None:
    """Verify native link mode uses the project target resource directory."""

    root = tmp_path / "AREDev"
    for directory in (
        root / "config",
        root / "are-resources" / "gff",
        root / "pgcc-resources" / "creature",
        root / "compiled-resources" / "are-dev-pgcc",
        root / "server" / "override",
        root / "server" / "modules",
        root / "data",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "config" / "arebuilder.env").write_text(
        "BUILD_TARGET=pgcc\n", encoding="utf-8"
    )
    (root / "pgcc-resources" / "creature" / "beast.utc").write_text(
        "target creature",
        encoding="utf-8",
    )
    (root / "compiled-resources" / "are-dev-pgcc" / "module.ifo").write_text(
        "module",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--root",
                str(root),
                "--builder-mount-root",
                "/var/builder",
                "link",
                "are-dev-pgcc",
            ]
        )
        == 0
    )

    _assert_resource_link(
        root / "server" / "override" / "beast.utc",
        symlink_target="/var/builder/pgcc-resources/creature/beast.utc",
        source_path=root / "pgcc-resources" / "creature" / "beast.utc",
    )


def test_quick_build_archives_module_ifo_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify quick builds prepare module state and archive module metadata."""

    _install_fake_script_compiler(monkeypatch)
    fixture = create_synthetic_fixture(tmp_path / "fixture")

    assert _run_builder(fixture, "quick", fixture.module_name) == 0

    assert {path.name for path in fixture.build_dir.iterdir()} == {
        "fixture_precompiled.ncs",
        "ignored.ncs",
        "module.ifo",
    }
    assert sorted(read_erf_members(fixture.module_archive_path)) == ["module.ifo"]
    assert (fixture.tlk_dir / "fixture.tlk").exists()
    assert (fixture.hak_dir / "fixture_hak.hak").exists()


def test_all_build_does_not_compile_shared_scripts(tmp_path: Path, monkeypatch) -> None:
    """Verify the all build compiles target scripts without running shared-script compile."""

    compile_calls = _install_fake_script_compiler(monkeypatch)
    fixture = create_synthetic_fixture(tmp_path / "fixture")
    _write_shared_script(fixture)

    assert _run_builder(fixture, "all", fixture.module_name) == 0

    assert len(compile_calls) == 1
    assert compile_calls[0]["script_dir"] == fixture.module_resources_dir / "scripts"
    assert not (fixture.compiled_resources_dir / "shared_direct.ncs").exists()
    _assert_resource_link(
        fixture.override_dir / "global_script.ncs",
        symlink_target="/var/builder/compiled-resources/global_script.ncs",
        source_path=fixture.compiled_resources_dir / "global_script.ncs",
    )


def test_direct_compile_uses_project_script_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify direct compile resolves scripts through the project layout."""

    compile_calls = _install_fake_script_compiler(monkeypatch)
    fixture = create_synthetic_fixture(tmp_path / "fixture")
    project_scripts = _write_shared_script(fixture)

    assert _run_builder(fixture, "compile", "all") == 0

    assert compile_calls[0]["script_dir"] == project_scripts
    assert compile_calls[0]["output_dir"] == fixture.compiled_resources_dir
    assert compile_calls[0]["state_path"] == fixture.root / "temp" / "script_index.json"
    assert compile_calls[0]["selector"] == "all"


def test_ci_changes_compile_without_project_layout_or_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compile the TC delta from any cwd, using the real compiler and CLI dispatch."""

    scripts = tmp_path / "checkout" / "sources"
    scripts.mkdir(parents=True)
    (scripts / "inc_shared.nss").write_text("int helper() { return 1; }")
    (scripts / "affected.nss").write_text(
        '#include "inc_shared"\nvoid main() { int x = helper(); }'
    )
    (scripts / "unaffected.nss").write_text("void main() {}")
    nwn = tmp_path / "game"
    (nwn / "ovr").mkdir(parents=True)
    (nwn / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    changes = tmp_path / "changes.txt"
    changes.write_text(
        "sources/inc_shared.nss:CHANGED:abc\nsources/removed.nss:REMOVED:def\n"
    )
    output = tmp_path / "output"
    state = tmp_path / "state.json"
    state.write_text("must remain untouched")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    assert (
        main(
            [
                "--script-dir",
                str(scripts),
                "--output-dir",
                str(output),
                "--nwn-root",
                str(nwn),
                "--state-file",
                str(state),
                "--changes",
                str(changes),
                "--ci",
                "compile",
            ]
        )
        == 0
    )

    assert {path.name for path in output.iterdir()} == {"affected.ncs"}
    assert (output / "affected.ncs").read_bytes().startswith(b"NCS V1.0")
    assert state.read_text() == "must remain untouched"
    log = capsys.readouterr()
    assert log.out == (
        "Compiling affected.nss\nCompilation complete.\n\n"
        "Stale build artifacts:\n- inc_shared.ncs\n- removed.ncs\n"
    )
    assert log.err == ""
    assert not (unrelated / "temp").exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--changes", "changes.txt", "all"], "--changes is only valid for compile"),
        (
            ["--changes", "changes.txt", "compile", "all"],
            "--changes cannot be combined",
        ),
    ],
)
def test_changes_rejects_non_compile_commands_and_selectors(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 1
    assert message in capsys.readouterr().out


def test_ci_and_changes_are_not_enabled_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "example.nss").write_text("void main() {}")
    game = tmp_path / "game"
    (game / "ovr").mkdir(parents=True)
    (game / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("COMPILE_CI", "true")
    monkeypatch.setenv("CHANGES", "/missing/changes.txt")
    monkeypatch.setenv("CHANGED_SCRIPTS", "/missing/changes.txt")
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "--script-dir",
                str(scripts),
                "--output-dir",
                str(tmp_path / "output"),
                "--nwn-root",
                str(game),
                "compile",
                "all",
            ]
        )
        == 0
    )
    log = capsys.readouterr().out
    assert "Indexing scripts..." in log
    assert "Compiling example.nss" not in log
    assert "Compilation complete." not in log


def test_target_compile_cleanup_preserves_precompiled_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed target source must expose its precompiled fallback, not delete it."""

    import json

    import arebuilder.app.arebuilder.engine as engine_module
    from arebuilder.nwn.compile import compile_scripts
    from arebuilder.tests.nwn.test_compile import fake_compiler_factory

    fixture = create_synthetic_fixture(tmp_path / "fixture")
    scripts = fixture.module_resources_dir / "scripts"
    (scripts / "fixture_precompiled.nss").write_text("void main() {}")
    fallback = fixture.root / "precompiled" / "fixture_precompiled.ncs"
    fallback.write_bytes(b"fallback")

    def compile_with_fake_backend(**kwargs: object) -> CompileResult:
        return compile_scripts(**kwargs, compiler_factory=fake_compiler_factory)

    monkeypatch.setattr(engine_module, "compile_scripts", compile_with_fake_backend)
    assert _run_builder(fixture, "quick", fixture.module_name) == 0
    (scripts / "fixture_precompiled.nss").unlink()
    assert _run_builder(fixture, "quick", fixture.module_name) == 0

    assert (fixture.build_dir / "fixture_precompiled.ncs").read_bytes() == b"fallback"
    state = fixture.root / "temp" / "script_index_synthetic.json"
    assert "fixture_precompiled" not in json.loads(state.read_text())["scripts"]


def test_cache_dir_reuses_native_outputs_in_a_fresh_output_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "example.nss").write_text("void main() { int x = 1; }")
    nwn = tmp_path / "game"
    (nwn / "ovr").mkdir(parents=True)
    (nwn / "ovr" / "nwscript.nss").write_text("void PrintString(string text);")

    def compile_all(output: Path) -> str:
        argv = ["--script-dir", str(scripts), "--output-dir", str(output)]
        argv += ["--nwn-root", str(nwn), "--state-file", str(tmp_path / "state")]
        argv += ["--cache-dir", str(tmp_path / "cache"), "--ci", "compile", "all"]
        assert main(argv) == 0
        return capsys.readouterr().out

    assert "Compiling example.nss" in compile_all(tmp_path / "first")
    assert compile_all(tmp_path / "second") == "Compilation complete.\n"
    assert (tmp_path / "second" / "example.ncs").read_bytes() == (
        tmp_path / "first" / "example.ncs"
    ).read_bytes()


@pytest.mark.parametrize(
    ("argv", "needs_fixture", "expected_message"),
    [
        (["unknown"], False, "Invalid command"),
        (
            ["compile", "one", "two"],
            False,
            "compile accepts at most one selector argument",
        ),
        (["all", "missing"], True, "Unknown target"),
    ],
    ids=["invalid-command", "compile-args", "unknown-target"],
)
def test_cli_bad_case_grid(
    tmp_path: Path,
    capsys,
    argv: list[str],
    needs_fixture: bool,
    expected_message: str,
) -> None:
    """Verify CLI bad-case inputs return expected diagnostics."""

    if needs_fixture:
        fixture = create_synthetic_fixture(tmp_path / "fixture")
        argv = ["--root", str(fixture.root), *argv]

    assert main(argv) == 1
    assert expected_message in capsys.readouterr().out


def _run_builder(fixture, *args: str) -> int:
    """Run the CLI builder command against a synthetic fixture."""

    return main(
        [
            "--root",
            str(fixture.root),
            "--builder-mount-root",
            "/var/builder",
            *args,
        ]
    )


def _write_shared_script(fixture) -> Path:
    """Create a shared NWScript source used by compile-path tests."""

    shared_scripts = fixture.are_resources_dir / "scripts"
    shared_scripts.mkdir()
    (shared_scripts / "shared_direct.nss").write_text(
        "void main() {}\n", encoding="latin-1"
    )
    return shared_scripts


def _install_fake_script_compiler(
    monkeypatch, *, emit_output: bool = False
) -> list[dict[str, object]]:
    """Install a fake script compiler and return the captured call list."""

    calls: list[dict[str, object]] = []

    def fake_compile_scripts(**kwargs) -> CompileResult:
        """Provide a fake implementation used to isolate this test case."""

        calls.append(kwargs)
        if emit_output:
            print("Indexing scripts...")
            print("\nAll 1 script(s) will be compiled.\n")
        script_dir = kwargs["script_dir"]
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        compiled_count = 0
        for script_path in sorted(script_dir.glob("*.nss")):
            text = script_path.read_text(encoding="latin-1")
            if "void main" not in text and "StartingConditional" not in text:
                continue
            (output_dir / f"{script_path.stem.lower()}.ncs").write_bytes(
                f"compiled:{script_path.stem.lower()}".encode("ascii")
            )
            compiled_count += 1
        return CompileResult(compiled_count=compiled_count)

    monkeypatch.setattr(
        "arebuilder.app.arebuilder.engine.compile_scripts",
        fake_compile_scripts,
    )
    return calls
