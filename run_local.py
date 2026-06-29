"""Local development launcher — equivalent to Docker PYTHONPATH=/app setup.

Docker mounts:
  Comp_Videos/tvd_pipeline → /app/tvd_pipeline   (importable as tvd_pipeline)
  api_pipeline             → /app/api_pipeline    (importable as api_pipeline)

This script mirrors that by adding both roots to sys.path before uvicorn starts.
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent
COMP_VIDEOS = ROOT / "Comp_Videos"

# Mirror Docker's PYTHONPATH=/app: both api_pipeline and tvd_pipeline importable
sys.path.insert(0, str(COMP_VIDEOS))   # tvd_pipeline lives here
sys.path.insert(0, str(ROOT))          # api_pipeline lives here

# Also set PYTHONPATH env var so uvicorn's reload subprocess inherits the same paths
_pypath = os.pathsep.join([str(ROOT), str(COMP_VIDEOS)])
existing = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = _pypath + (os.pathsep + existing if existing else "")

# Load .env from api_pipeline/ (same as Docker env_file)
env_file = ROOT / "api_pipeline" / ".env"
if env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(env_file, override=True)

# Set credentials paths (matching docker-compose.yml)
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(COMP_VIDEOS / "service_account.json"),
)
os.environ.setdefault(
    "GCS_CREDENTIALS_FILE",
    str(COMP_VIDEOS / "service_account.json"),
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_pipeline.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[
            str(ROOT / "api_pipeline"),
            str(COMP_VIDEOS / "tvd_pipeline"),
        ],
        reload_excludes=["*.jpg", "*.jpeg", "*.png", "*.mp4", "*.mp3", "*.wav"],
    )
