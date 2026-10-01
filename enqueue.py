import sys

from conduit import call


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
    with open(path) as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    for url in urls:
        status, body = call("/api/jobs", {
            "task": {"name": "check", "queue": "links", "max_retries": 2, "payload": {"url": url}},
        })
        if status != 201:
            sys.exit(f"enqueue {url}: {status} {body}")
        print(body["id"], url)


if __name__ == "__main__":
    main()
