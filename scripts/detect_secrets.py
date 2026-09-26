#!/usr/bin/env python3
import sys
import re

SECRET_PATTERN = re.compile(
    r'([A-Za-z0-9_]*(key|token|secret)[A-Za-z0-9_]*\s*[:=]\s*[\"\'\\]?[A-Za-z0-9_\-]{20,}[\"\'\\]?|BEGIN (RSA )?PRIVATE KEY)',
    re.IGNORECASE
)

def check_file(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                if SECRET_PATTERN.search(line):
                    print(f"Secret detected in {filename} at line {line_num}")
                    return True
    except Exception:
        pass
    return False

if __name__ == "__main__":
    found_secrets = False
    for filename in sys.argv[1:]:
        if check_file(filename):
            found_secrets = True
    if found_secrets:
        sys.exit(1)
    sys.exit(0)
