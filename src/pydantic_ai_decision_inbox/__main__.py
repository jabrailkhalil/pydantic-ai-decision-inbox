import argparse
import os
import sys
from pathlib import Path

from .lab import SCENARIOS, run_scenario
from .server import make_server


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local PydanticAI decision and crash lab"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("scenario", choices=SCENARIOS)
    run.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="Fresh empty directory; no existing files are overwritten",
    )
    serve = commands.add_parser("serve")
    serve.add_argument("--directory", type=Path, default=Path("dist"))
    serve.add_argument("--workspace", type=Path, default=Path("demo-work"))
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    try:
        if args.command == "run":
            print(run_scenario(args.scenario, args.directory).model_dump_json(indent=2))
        else:
            if not 0 < args.port < 65536:
                raise ValueError("Port must be 1–65535")
            server = make_server(args.directory, args.workspace, args.port)
            print(
                f"Local lab: http://127.0.0.1:{server.server_address[1]}/?live=1",
                flush=True,
            )
            try:
                server.serve_forever()
            finally:
                server.server_close()
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
