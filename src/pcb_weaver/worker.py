"""Run the durable engineering worker independently of an MCP connection."""
import argparse
import signal

from .jobs import JobQueue
from .runtime import load_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--config")
    args = parser.parse_args()
    root, config = load_runtime(args.workspace, args.config)
    queue = JobQueue(root, config)

    def stop(signum, frame):
        queue.stop_event.set()
        queue.wake.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    queue.start()
    try:
        while queue.thread.is_alive():
            queue.thread.join(timeout=1)
    finally:
        queue.close()
        queue.thread.join()


if __name__ == "__main__":
    main()
