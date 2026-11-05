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
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    # gh uses exit code 1 for both 4xx and 5xx. Its HTTP diagnostic is on
    # stderr ("gh: Server Error (HTTP 502)"), not the JSON response body.
    diagnostic = error.stderr or ""
    status = re.search(r"^gh: .+ \(HTTP ([0-9]{3})\)\r?$", diagnostic, re.MULTILINE)
    if status:
        return 500 <= int(status[1]) < 600
    # Go's transport can time out before the subprocess deadline. Match only
    # timeout diagnostics from our GET, not auth/certificate errors or arbitrary
    # mentions of timeouts. Connection reset/refused are deliberately not added.
    return re.search(
        r'^Get "https://[^"\r\n]+": (?:net/http: TLS handshake timeout|'
        r'net/http: timeout awaiting response headers|[^\r\n]+: i/o timeout|'
        r'context deadline exceeded(?: \(Client.Timeout exceeded while awaiting headers\))?|'
        r'net/http: request canceled \(Client.Timeout exceeded while awaiting headers\))\r?$',
        diagnostic, re.MULTILINE,
    ) is not None


def temporary_url_error(error):
    return (isinstance(error, urllib.error.HTTPError) and 500 <= error.code < 600
            or isinstance(error, TimeoutError)
            or isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError))
