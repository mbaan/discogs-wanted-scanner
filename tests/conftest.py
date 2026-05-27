import sys
from pathlib import Path

# Allow tests to import top-level modules (watcher, shop_api, evaluator, ...)
sys.path.insert(0, str(Path(__file__).parent.parent))
