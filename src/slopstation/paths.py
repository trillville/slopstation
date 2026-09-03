"""Where the RUNTIME data lives: config.json, secrets.json, state/, logs/,
couch.log, media/.env.

An installed package can be imported from anywhere, so this is no longer
"beside the code" - it is a checkout, and the two only coincide because the
install is editable. SLOPSTATION_HOME overrides it; anything that runs the
package from outside a checkout has to set that.
"""

import os
import pathlib

# src/slopstation/paths.py -> the checkout root.
_CHECKOUT = pathlib.Path(__file__).resolve().parents[2]

HOME = pathlib.Path(os.environ.get("SLOPSTATION_HOME") or _CHECKOUT)
