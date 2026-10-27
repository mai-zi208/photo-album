"""Launch the local server. Usage:  .venv\\Scripts\\python.exe run.py"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.server:app", host="127.0.0.1", port=8765, log_level="warning")
