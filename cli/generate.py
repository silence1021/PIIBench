#!/usr/bin/env python3
"""
Generate seed records and HTML pages with the PIIGuard pipeline.

Run:
  python -m cli.generate --help
"""

from defipi.generation.pipeline_piiguard import main


if __name__ == "__main__":
    main()
