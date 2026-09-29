"""Command line: run the server, or check a config file."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from .config import ConfigError, load_config


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contact-form-backend",
        description="Self-hosted contact form endpoint with CAPTCHA-free spam protection.",
    )
    parser.add_argument(
        "--config", default=os.environ.get("CFB_CONFIG", "forms.toml"),
        help="TOML config file (default: $CFB_CONFIG or forms.toml)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="validate the config and exit")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    if args.check:
        for fid, form in config.forms.items():
            names = ", ".join(
                s.name + ("*" if s.required else "") + ("" if s.type == "text" else ":" + s.type)
                for s in form.fields
            )
            print("POST /f/%s  token=%s  fields: %s" % (fid, form.token, names))
        print("config OK (%d form(s))" % len(config.forms))
        return 0

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import uvicorn

    from .app import create_app

    try:
        app = create_app(config)
    except ValueError as exc:
        print("startup error: %s" % exc, file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
