"""Plan host seam shared by native and bundled delegated tools."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import partial

# Set by SessionRuntime before creating the provider pump; adapter snapshots it
# on every submit, including pooled engines. Tool calls cannot mint a new proof.
CURRENT_PLAN_RUN = ContextVar("session_plan_run", default=None)


@contextmanager
def run_context(recorder):
    """Publish only the run this producer actually claimed; never inherit one."""
    proof = None
    if recorder and recorder.run_id is not None and recorder.fence_token is not None:
        proof = (recorder.session_id, recorder.run_id, recorder.fence_token)
    token = CURRENT_PLAN_RUN.set(proof)
    try:
        yield
    finally:
        CURRENT_PLAN_RUN.reset(token)


@asynccontextmanager
async def recorded_turn(session_id, prompt, profile_id, source=None):
    """HTTP/headless producers own and release the same fenced recorder as API background turns."""
    from app.core.turn_recorder import TurnRecorder

    recorder = await TurnRecorder.maybe_create(session_id)
    try:
        with run_context(recorder):
            if recorder:
                await recorder.start(prompt, profile_id=profile_id, source=source)
            yield recorder
    finally:
        if recorder:
            await recorder.close()


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
