"""Start the manager's dashboard.

    py dashboard.py

Then open http://127.0.0.1:5000 in a browser on the same machine.

It listens on localhost only. Anyone who can reach the dashboard can change
where files are filed, so putting it on the campus network is a decision
someone should take deliberately: set dashboard_host to 0.0.0.0 in
settings.yaml if you really want that.
"""

from __future__ import annotations

from src import config, db
from src.dashboard import create_app

if __name__ == "__main__":
    cfg = config.load_config()
    config.ensure_dirs()
    db.init_db()

    host = str(cfg.get("dashboard_host", "127.0.0.1"))
    port = int(cfg.get("dashboard_port", 5000))

    print(f"Dashboard running at http://{host}:{port}  (Ctrl-C to stop)")
    create_app(cfg).run(host=host, port=port, debug=False)
