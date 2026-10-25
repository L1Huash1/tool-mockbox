"""Call the healthy fixture server using only Python's standard library."""

import argparse
import http.client
import json


def main():
    parser = argparse.ArgumentParser(description="Call tool-mockbox's local healthy fixture")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be from 1 to 65535")
    body = json.dumps({"tool": "lookup", "arguments": {"id": "demo"}}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=10)
    try:
        connection.request("POST", "/call", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        print(f"HTTP {response.status}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if response.status == 200 and result.get("ok") else 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
