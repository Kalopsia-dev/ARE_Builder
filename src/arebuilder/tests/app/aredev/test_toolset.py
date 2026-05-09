from pathlib import Path

from arebuilder.tests.app.aredev.helpers import (
    assert_toolset_resource,
    make_controller,
    stage_toolset_sources,
)


def test_toolset_requires_built_module(tmp_path: Path) -> None:
    """Verify toolset bundling fails clearly before resources are linked."""

    output: list[str] = []
    controller, _, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
    )

    assert controller.run("toolset", []) == 1

    assert output == ["You must build the module first."]
    assert build_calls == []
    assert runner.calls == []


def test_toolset_creates_bundle_in_nwn_home_modules(tmp_path: Path) -> None:
    """Verify toolset links resources by basename and copies the built module."""

    nwn_home = tmp_path / "nwn-home"
    output: list[str] = []
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
        nwn_home=nwn_home,
    )
    sources = stage_toolset_sources(layout)

    assert controller.run("toolset", []) == 0

    module_dir = nwn_home / "modules" / "are-dev-pgcc"
    assert_toolset_resource(module_dir / "shared_only.are", sources["shared_only"])
    assert_toolset_resource(module_dir / "shared.are", sources["target_shared"])
    assert_toolset_resource(module_dir / "include.nss", sources["script"])
    assert_toolset_resource(module_dir / "target.utc", sources["target"])
    assert_toolset_resource(module_dir / "module.ifo", sources["compiled"])
    assert (nwn_home / "modules" / "are-dev-pgcc.mod").read_bytes() == b"module"
    assert output == [
        "Planning symlinks...",
        "Toolset bundle ready.",
    ]
    assert build_calls == []
    assert runner.calls == []


def test_toolset_bad_argument_reports_usage(tmp_path: Path) -> None:
    """Verify invalid toolset arguments report usage without running workflows."""

    output: list[str] = []
    controller, _, build_calls, runner = make_controller(tmp_path, output=output.append)

    assert controller.run("toolset", ["open"]) == 1

    assert build_calls == []
    assert runner.calls == []
    assert any("Usage: toolset [run]" in message for message in output)
