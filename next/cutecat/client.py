"""External CLI client: always talks to the service, never opens its database."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .config import environment

COMMANDS = {"engines", "templates", "validate", "submit", "jobs", "status", "logs", "start", "pause", "cancel"}


def request(url, path, *, token="", body=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("server must be an http(s) origin without credentials/query/fragment")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url.rstrip("/") + "/api/v1/" + path, data=data, headers=headers)
    try:
        # Never forward a bearer token to a redirect target.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            try:
                error = json.load(exc).get("error", "request failed")
            except (ValueError, AttributeError):
                error = "request failed"
            raise ValueError(f"HTTP {exc.code}: {error}") from exc
        finally:
            exc.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="springhub", description="SpringHub HTTP API client (JSON output)")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("id", nargs="?")
    parser.add_argument("--server", default=environment("URL", "http://127.0.0.1:8080"))
    parser.add_argument("--engine", choices=("handbrake", "ffmpeg", "rffmpeg"))
    parser.add_argument("--template", help="task template UUID")
    parser.add_argument("--spec", help="structured spec JSON file (no argv)")
    parser.add_argument("--input-root")
    parser.add_argument("--input-path")
    parser.add_argument("--output-root")
    parser.add_argument("--output-path")
    parser.add_argument("--wait", action="store_true", help="wait for submitted job's terminal status")
    parser.add_argument("--wait-timeout", type=float, default=3600)
    args = parser.parse_args(argv)
    token = environment("API_TOKEN", "", allow_empty=True)
    try:
        if args.wait and (args.command != "submit" or args.wait_timeout <= 0):
            raise ValueError("--wait requires submit and a positive --wait-timeout")
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else None
        if spec is not None and not isinstance(spec, dict):
            raise ValueError("spec file must contain an object")
        if args.command in ("validate", "submit"):
            body = {}
            if args.engine:
                body["engine"] = args.engine
            if args.template:
                body["template_id"] = args.template
            if spec is not None:
                body["spec"] = spec
            if args.command == "submit":
                for key in ("input_root", "input_path", "output_root", "output_path"):
                    if not getattr(args, key):
                        raise ValueError("submit requires --input-root/path and --output-root/path")
                body.update(input={"root": args.input_root, "path": args.input_path},
                            output={"root": args.output_root, "path": args.output_path})
            result = request(args.server, "spec/validate" if args.command == "validate" else "jobs", token=token, body=body)
        elif args.command in ("status", "logs", "start", "pause", "cancel"):
            if not args.id:
                raise ValueError("job UUID is required")
            path = "jobs/" + urllib.parse.quote(args.id, safe="")
            if args.command != "status":
                path += "/" + args.command
            result = request(args.server, path, token=token, body={} if args.command in ("start", "pause", "cancel") else None)
        else:
            result = request(args.server, "task-templates" if args.command == "templates" else args.command, token=token)
        if args.wait:
            deadline = time.monotonic() + args.wait_timeout
            while result["status"] not in ("succeeded", "failed", "canceled", "interrupted"):
                if time.monotonic() >= deadline:
                    print(json.dumps(result, ensure_ascii=False))
                    print("wait timed out; job was not canceled", file=sys.stderr)
                    return 3
                time.sleep(.5)
                result = request(args.server, "jobs/" + result["id"], token=token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("status") in ("failed", "canceled", "interrupted") else 0
    except (ValueError, OSError, urllib.error.URLError) as exc:
        message = str(exc)
        if token:
            message = message.replace(token, "[redacted]")
        print(message, file=sys.stderr)
        return 2
