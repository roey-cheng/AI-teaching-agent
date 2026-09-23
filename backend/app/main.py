from fastapi import FastAPI

app = FastAPI(title="AI Teaching Assistant")


@app.get("/health")
def health() -> dict[str, str]:
    """Check that the HTTP application is responding, not DB/model readiness."""
    return {"status": "ok"}
