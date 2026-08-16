from pathlib import Path

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELOAD_PATTERNS = ["*.py", "*.html", "*.css", "*.js"]


def main():
    uvicorn.run(
        "web.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[str(PROJECT_ROOT)],
        reload_includes=RELOAD_PATTERNS,
    )


if __name__ == "__main__":
    main()
