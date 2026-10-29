# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""Child process used by the resource tests: run one public-module call and report peak memory, time and outcome as JSON.

``python tests/security/probe_child.py <server-key> <profile> <function> '<json kwargs>' [address-space-limit-MB]``
"""

import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.security import harness  # noqa: E402  pylint: disable=wrong-import-position


def main():
    key, profile, function, raw = sys.argv[1:5]
    if len(sys.argv) > 5:
        limit = int(sys.argv[5]) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    server = harness.Server(key)
    kwargs = json.loads(raw)
    started = time.time()
    with harness.injected(server.profiles()) as module:
        kind, value = harness.attempt(lambda: getattr(module, function)(profile, **kwargs))
    out = {
        "kind": kind,
        "seconds": round(time.time() - started, 2),
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
    }
    if kind == "OK":
        out.update(
            row_count=value.get("row_count"),
            truncated=value.get("truncated"),
            rows=len(value.get("rows", [])),
        )
    else:
        out["error"] = f"{type(value).__name__}: {str(value)[:160]}"
    print("RESULT " + json.dumps(out))


if __name__ == "__main__":
    main()
