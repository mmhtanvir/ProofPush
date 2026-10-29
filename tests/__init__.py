import sys
from pathlib import Path

# Add project root and tests directory to sys.path
root_dir = Path(__file__).resolve().parent.parent
tests_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(tests_dir) not in sys.path:
    sys.path.insert(0, str(tests_dir))
