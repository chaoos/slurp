"""The checks of batch scripts and jobs: engine.py (findings, the facts of a cluster, the rule catalogue), rules.py (the
rules, which register themselves in engine.RULES; what sites add, the site packs: slurp.sites),
fixes.py (the safe fixes), environment.py and placement.py (the environment and placement checks), guides.py and
templates.py (the guides and the job script templates)."""

from . import engine, rules # noqa: F401 (rules registers the rules in engine.RULES: any use of the checks gets them)
