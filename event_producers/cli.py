import typer, os, subprocess, json, shlex, pathlib, httpx, sys, time
from datetime import datetime, timezone
from .config import settings

app = typer.Typer(help="bloodbank CLI")


def detect_project_and_cwd():
    cwd = os.getcwd()
    # try resolve git root
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
        project = pathlib.Path(root).name
    except Exception:
        project = None
    return project, cwd


@app.command()
def publish_prompt(provider: str, model: str = "", prompt: str = typer.Argument(...)):
    project, cwd = detect_project_and_cwd()
    payload = {"provider": provider, "model": model or None, "prompt": prompt}
    env = {"project": project, "working_dir": cwd, "domain": None, "tags": ["cli"]}
    body = payload | env
    r = httpx.post("http://localhost:8682/events/agent/thread/prompt", json=payload)
    typer.echo(r.json())


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def wrap(ctx: typer.Context, program: str, provider: str = "other"):
    """
    Wrap an LLM CLI and siphon its stdin/stdout.
    Usage: bb wrap claude -- <original args...>
    """
    project, cwd = detect_project_and_cwd()

    # capture input on stdin (prompt) if present
    # You can get fancier per tool, but this works generically for "echo '...' | tool"
    stdin_data = sys.stdin.read() if not sys.stdin.isatty() else None
    if stdin_data and stdin_data.strip():
        httpx.post(
            "http://localhost:8682/events/agent/thread/prompt",
            json={"provider": provider, "prompt": stdin_data, "model": None},
        )

    # run the original program pass-through
    cmd = [program] + ctx.args
    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_data else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        if stdin_data:
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        response_chunks = []
        for line in proc.stdout:
            response_chunks.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait()

    if response_chunks:
        httpx.post(
            "http://localhost:8682/events/agent/thread/response",
            json={
                "provider": provider,
                "prompt_id": "unknown",  # if you want, stash the ID from prompt call above
                "response": "".join(response_chunks),
            },
        )


if __name__ == "__main__":
    app()
