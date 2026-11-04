import sys
from pathlib import Path

from arebuilder.app.aredev.process import default_process_runner


def test_captured_output_survives_undecodable_bytes(tmp_path: Path) -> None:
    """Bytes that are not valid text are replaced instead of losing the output."""

    script = (
        "import sys; sys.stdout.buffer.write(b'before \\x81\\xff after\\n'); "
        "sys.stdout.flush()"
    )
    result = default_process_runner(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=None,
        capture_output=True,
        background=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("before ")
    assert result.stdout.rstrip().endswith(" after")
    assert "�" in result.stdout
