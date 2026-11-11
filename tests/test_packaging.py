import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_wheel_contents_and_entry_point(tmp_path):
    out = tmp_path / "wheelhouse"
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-w", str(out),
         "--no-build-isolation" if _has_setuptools() else "--quiet"],
        check=True, capture_output=True, timeout=300,
    )
    wheels = list(out.glob("expenser-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as z:
        names = set(z.namelist())
        assert "expenser/cli.py" in names
        flows = ROOT / "expenser" / "flows"
        if flows.is_dir():
            for y in flows.glob("*.yaml"):
                assert f"expenser/flows/{y.name}" in names
        ep = next(n for n in names if n.endswith("entry_points.txt"))
        assert "expenser = expenser.cli:app" in z.read(ep).decode()


def _has_setuptools() -> bool:
    try:
        import setuptools  # noqa: F401
        import wheel  # noqa: F401

        return True
    except ImportError:
        return False
