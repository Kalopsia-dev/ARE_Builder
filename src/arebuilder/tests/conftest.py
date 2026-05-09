import sys
from pathlib import Path

AREDEV_ROOT = Path(__file__).resolve().parents[3]
source_root = AREDEV_ROOT / "src"
source_root_text = str(source_root)
if source_root.exists() and source_root_text not in sys.path:
    sys.path.insert(0, source_root_text)
