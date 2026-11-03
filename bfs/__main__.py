"""``python -m bfs`` 等价于 ``bfs.cli``。"""

import sys

from .cli import main

sys.exit(main())
