"""In-process render job queue.

One daemon worker thread renders jobs sequentially (rendering is CPU-bound;
serializing avoids thrashing the machine mid-service). The Flask handlers
submit jobs and poll status; everything is guarded by one lock.
"""

import queue
import threading
import uuid


class Job:
    def __init__(self, job_type, options):
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type
        self.options = options
        self.status = "queued"      # queued | rendering | done | error
        self.progress = 0           # 0..100
        self.filename = None
        self.error = None

    def to_dict(self, queue_position=0):
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "filename": self.filename,
            "error": self.error,
            "queue_position": queue_position,
        }


# Terminal (done/error) jobs kept around for status polls; older ones are
# evicted on submit so _jobs can't grow without bound in a long-lived server.
KEEP_FINISHED = 50


class JobManager:
    """renderers: {"timer": fn, "spinner": fn} where
    fn(options, progress_cb) -> output filename (basename in exports/).
    progress_cb accepts an int 0..100.
    """

    def __init__(self, renderers):
        self._renderers = renderers
        self._jobs = {}
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, job_type, options):
        if job_type not in self._renderers:
            raise ValueError(f"unknown visual type: {job_type}")
        job = Job(job_type, options)
        with self._lock:
            finished = [j.id for j in self._jobs.values()
                        if j.status in ("done", "error")]
            for stale_id in finished[:-KEEP_FINISHED]:
                del self._jobs[stale_id]
            self._jobs[job.id] = job
        self._queue.put(job)
        return job.id

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            position = 0
            if job.status == "queued":
                # _jobs preserves insertion order (submit() inserts under this
                # same lock), which matches the worker's FIFO queue order.
                queued_ids = [j.id for j in self._jobs.values()
                              if j.status == "queued"]
                position = queued_ids.index(job.id) + 1 \
                    if job.id in queued_ids else 0
            return job.to_dict(queue_position=position)

    def _run(self):
        while True:
            job = self._queue.get()
            with self._lock:
                job.status = "rendering"

            def progress_cb(pct, _job=job):
                with self._lock:
                    _job.progress = max(0, min(100, int(pct)))

            try:
                filename = self._renderers[job.type](job.options, progress_cb)
                with self._lock:
                    job.filename = filename
                    job.progress = 100
                    job.status = "done"
            except Exception as exc:  # surface anything to the UI
                with self._lock:
                    job.error = str(exc) or exc.__class__.__name__
                    job.status = "error"
