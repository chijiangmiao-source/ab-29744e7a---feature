"""Entrypoint: python -m app.serve [--host 0.0.0.0] [--port 8080]"""

from __future__ import annotations

import argparse
import os

from .api import build_server
from .storage import FrozenStore


def main() -> int:
    ap = argparse.ArgumentParser(description="Boot-slot sector recovery audit service")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    args = ap.parse_args()

    store = FrozenStore(os.path.join(args.data, "audits.json"))
    httpd = build_server(args.host, args.port, store)
    print(f"slot-audit listening on {args.host}:{args.port} (data={args.data})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
