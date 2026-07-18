#!/usr/bin/env python3
import sys

from coop_door import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] + ["open"]))
