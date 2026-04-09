"""Spark dashboard — local web monitoring UI.

Launch with::

    python spark/init.py --dashboard [--port 8383]
"""

from __future__ import annotations

import os
import webbrowser

from spark.tools.install_templates import PLATFORM_MAP, detect_platform


def run_dashboard(target_dir: str, port: int = 8383) -> int:
    """Launch the Spark monitoring dashboard and block until Ctrl+C."""
    # Resolve paths
    platform = detect_platform(target_dir)
    plat_dir = os.path.join(target_dir, PLATFORM_MAP[platform]["dir"])
    db_path = os.path.join(plat_dir, "spark.db")

    if not os.path.isfile(db_path):
        print(f"[spark] No database found at {db_path}")
        print("[spark] Run 'python spark/init.py --index-only' first to scan files.")
        return 1

    log_path = os.path.join(plat_dir, "jcodemunch.log")
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")

    # Import here to keep top-level lightweight
    from spark.dashboard.server import create_server

    server = create_server(
        db_path=db_path,
        log_path=log_path,
        templates_dir=templates_dir,
        host="127.0.0.1",
        port=port,
        target_dir=target_dir,
    )

    url = f"http://127.0.0.1:{port}/overview"
    print(f"[spark] Dashboard running at {url}")
    print("[spark] Press Ctrl+C to stop.")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[spark] Dashboard stopped.")
    finally:
        server.server_close()

    return 0
