"""Run API and worker in one application container, with fail-fast supervision."""
import os
import signal
import subprocess
import sys
import threading
import time


def commands():
    result = [[sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0',
               '--port', '8055', '--ws-max-size', '65536', '--no-access-log']]
    if os.environ.get('SETAPI_RUN_WORKER', 'true').lower() == 'true':
        result.append([sys.executable, '-m', 'app.worker'])
    return result


def supervise(child_commands, stop=None, grace=20):
    stop = stop or threading.Event()
    children = []
    exit_code = 0
    try:
        for command in child_commands:
            children.append(subprocess.Popen(command, start_new_session=True))
        while not stop.wait(.2):
            if any(child.poll() is not None for child in children):
                exit_code = 1
                break
    finally:
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        for child in children:
            try:
                child.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
    return exit_code


def main():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return supervise(commands(), stop)


if __name__ == '__main__':
    sys.exit(main())
