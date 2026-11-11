"""Click around the real page with no Gemini and no credentials:  uv run tests/demo_server.py

The agents are replaced by the scripted stand-ins from test_pipeline.py, so the page, the research
board, the clarifying question, citation tags and the sources all work against the real data. The
report's wording is canned: this checks the plumbing, not the answers. Open http://localhost:8000.
Try the dashed "Is the shelter doing better?" example to see the clarifying question.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

import app
import test_pipeline

if __name__ == "__main__":
    test_pipeline.install()
    print("Demo with scripted agents: http://localhost:8000")
    uvicorn.run(app.app, host="127.0.0.1", port=8000, log_level="warning")
