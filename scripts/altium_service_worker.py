"""Run durable Altium developer-beta jobs outside the MCP process."""
import argparse
import json
import signal
import sys
import threading

from altium_service import AltiumService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=1.0)
    args = parser.parse_args()
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error('--poll-seconds must be between 0.1 and 60')
    service = AltiumService()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        job = service.run_once()
        if job is not None:
            print(json.dumps({'id': job['id'], 'state': job['state'], 'error': job['error']},
                             ensure_ascii=False), file=sys.stderr, flush=True)
        if args.once:
            break
        if job is None:
            stop.wait(args.poll_seconds)


if __name__ == '__main__':
    main()
