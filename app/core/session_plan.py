"""Plan host seam shared by native and bundled delegated tools."""
from contextvars import ContextVar
from functools import partial

# Set by SessionRuntime before creating the provider pump; adapter snapshots it
# on every submit, including pooled engines. Tool calls cannot mint a new proof.
CURRENT_PLAN_RUN = ContextVar("session_plan_run", default=None)


class Backend:
    def __init__(self, session_id, proof):
        self.session_id, self.proof = session_id, proof

    def capture(self):
        from app.core.config import settings
        from app.db.session_plans import execute
        from app.network.identity import load
        return partial(execute, self.session_id(), device_id=load().device_id,
                       runner_id=settings.effective_instance_id, proof=self.proof())

    def __call__(self, args):
        return self.capture()(args)
