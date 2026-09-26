# Otsutsuki Harness

## Usage
To run the harness on a separate target repository, use:
`TARGET_REPO=/path/to/other/repo make run`
If `TARGET_REPO` is not provided, the harness will operate on its own directory by default.

## Pre-commit Secret Scanning
To enable the pre-commit secret-scanning hook, run:
`pip install pre-commit && pre-commit install`
