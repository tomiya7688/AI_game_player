"""Optional developer comparison sampler; sampled RSS/child CPU are not OS hard budgets."""

import threading
import time


class PlanningResourceMonitor:
    def __init__(self, interval_seconds=0.05):
        import psutil

        self.process = psutil.Process()
        self.process_errors = (psutil.Error, OSError)
        if not 0.01 <= interval_seconds <= 0.25:
            raise ValueError("resource interval must be .01.. .25 seconds")
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = None
        self.samples = 0
        self.peak_rss = 0
        self.child_cpu = {}
        self.error = None
        self.started_cpu = time.process_time()

    def sample(self):
        try:
            processes = [self.process, *self.process.children(recursive=True)]
            rss = 0
            for process in processes:
                try:
                    rss += process.memory_info().rss
                    if process.pid != self.process.pid:
                        key = (process.pid, process.create_time())
                        cpu = process.cpu_times()
                        self.child_cpu[key] = max(
                            self.child_cpu.get(key, 0), cpu.user + cpu.system
                        )
                except self.process_errors:
                    continue
            self.peak_rss = max(self.peak_rss, rss)
            self.samples += 1
        except Exception as error:
            self.error = type(error).__name__

    def start(self):
        self.sample()

        def loop():
            while not self.stop_event.wait(self.interval_seconds):
                self.sample()

        self.thread = threading.Thread(
            target=loop, daemon=True, name="planning-resource-sampler"
        )
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.sample()
        return {
            "parent_cpu_seconds": time.process_time() - self.started_cpu,
            "child_cpu_seconds_sampled_lower_bound": sum(self.child_cpu.values()),
            "process_tree_peak_rss_bytes_sampled": self.peak_rss,
            "samples": self.samples,
            "interval_seconds": self.interval_seconds,
            "sampling_error": self.error,
            "semantics": "50ms process-tree samples (browser+worker); short-lived/tail CPU may be missed, shared RSS may be counted twice; not OS hard-budget evidence",
        }
