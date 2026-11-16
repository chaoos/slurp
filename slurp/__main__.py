#! /usr/bin/env python3
"""Start slurp: python3 slurp [options] (the package directory) or python3 -m slurp [options]"""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0] or ".") == here: # run as python3 slurp: the package dir is not a module dir
  sys.path.pop(0)
sys.path.insert(0, os.path.dirname(here)) # the repository, where the package is

from slurp.cli import main

main()
