"""Small, fixed retries for publication's read-only HTTP evidence requests."""
import re
import subprocess
import time
import urllib.error


def retry_read(read, temporary):
    # Three attempts, with only three seconds of backoff in total. Each caller
    # retains its existing request timeout; the final error always propagates.
    for attempt in range(3):
        try:
            return read()
        except Exception as error:
            if attempt == 2 or not temporary(error):
                raise
            time.sleep(attempt + 1)


def temporary_command_error(error):
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    # gh uses exit code 1 for both 4xx and 5xx. Its HTTP diagnostic is on
    # stderr ("gh: Server Error (HTTP 502)"), not the JSON response body.
    return (isinstance(error, subprocess.CalledProcessError)
            and re.search(r"^gh: .+ \(HTTP 5[0-9]{2}\)\r?$", error.stderr or "", re.MULTILINE) is not None)


def temporary_url_error(error):
    return (isinstance(error, urllib.error.HTTPError) and 500 <= error.code < 600
            or isinstance(error, TimeoutError)
            or isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError))
