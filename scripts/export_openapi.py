"""Export the HTTP contract without starting workers or making network requests."""

import json
from pathlib import Path

from paper_search.api.workbench import create_app

target = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
target.write_text(
    json.dumps(create_app(lifespan=None).openapi(), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(target)
