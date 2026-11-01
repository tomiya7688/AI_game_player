"""Private reference-game HTTP server with finite startup burst headroom."""

from http.server import ThreadingHTTPServer


class ReferenceGameHTTPServer(ThreadingHTTPServer):
    """Keep the existing server lifecycle/protocol; size only the pre-accept queue.

    One page, one CSS and ten scripts may arrive in a startup burst while the
    accept thread is descheduled. 32 gives that finite fixture headroom above
    the Python 3.10-3.14 default of five. This requests a listen backlog of 32;
    the OS may clamp or round it. It is not a hard bound on connection count,
    memory, or active daemon handlers, and does not promise browser load success.
    CPython changed its default to SOMAXCONN in commit
    92f85ff3a07335e32a0e00a55b7b6aaf3657019b (GH-134249 / issue 80184).
    """

    request_queue_size = 32
