import threading
import time
from .context import resolve

# Read-only discovery/readback and explicit close have no foreground CAD
# command, so ESC cannot recover them and may cancel a modal in an older
# drawing. Timeout/modal still means unknown; the drawing-command watchdog
# below is unchanged.
NO_ESCAPE_STEPS = {'document.readback', 'document.list', 'document.close'}


class Engine:
    def __init__(self, store, adapter, recovery, poll=0.1):
        self.store, self.adapter, self.recovery = store, adapter, recovery
        self.poll = poll
        self.quarantined = False

    def run_one(self):
        if self.quarantined or self.store.has_uncertainty():
            return False
        job = self.store.claim()
        if not job:
            return False
        # In-memory drawing ownership never crosses a job boundary. The
        # adapter's document (if any) is not closed here; close is explicit.
        reset = getattr(self.adapter, 'reset_job', None)
        if reset is not None:
            reset()
        context, results = job['context'], job['results']
        for step in job['recipe']['steps']:
            if self.store.get(job['id'])['cancel']:
                self.store.finish(job['id'], 'cancelled', context, results)
                return True
            done, aborted = threading.Event(), threading.Event()
            evidence = {}
            began = time.monotonic()
            no_escape = step['op'] in NO_ESCAPE_STEPS

            def watchdog():
                while not done.wait(self.poll):
                    try:
                        reason = 'modal' if self.recovery.modal() else None
                        if time.monotonic() - began >= step.get('timeout_s', 30):
                            reason = 'timeout'
                        if reason:
                            evidence['reason'] = reason
                            aborted.set()
                            self.quarantined = True
                            if no_escape:
                                evidence['escape'] = ('skipped: step has no '
                                                      'foreground command')
                                self.store.finish(job['id'], 'unknown_outcome', context, results,
                                                  f'{step["id"]}: {reason}; no foreground command, '
                                                  'outcome requires inspection; no ESC sent')
                                return
                            # Persist uncertainty before ESC; CAD may already have changed.
                            self.store.finish(job['id'], 'unknown_outcome', context, results,
                                              f'{step["id"]}: {reason}; ESC requested, outcome requires inspection')
                            for _ in range(3):
                                evidence['escape'] = self.recovery.escape()
                                if done.wait(0.2):
                                    break
                            return
                    except Exception as exc:
                        evidence['watchdog_error'] = str(exc)
                        aborted.set()
                        self.quarantined = True
                        self.store.finish(job['id'], 'unknown_outcome', context, results, str(exc))
                        return

            results.append({'id': step['id'], 'op': step['op'], 'state': 'running'})
            self.store.finish(job['id'], 'running', context, results)
            watcher = threading.Thread(target=watchdog, daemon=True)
            watcher.start()
            value, error, uncertain = None, None, False
            try:
                value = self.adapter.execute(step['op'], resolve(step.get('args', {}), context))
            except Exception as exc:
                error = str(exc)
                uncertain = bool(getattr(exc, 'uncertain', False))
                if getattr(exc, 'command_completion_timeout', False) and not aborted.is_set():
                    # The adapter's completion deadline may precede the recipe
                    # deadline. Persist uncertainty before stopping watchdog/ESC.
                    self.quarantined = True
                    aborted.set()
                    evidence['reason'] = 'command_completion_timeout'
                    self.store.finish(job['id'], 'unknown_outcome', context, results, error)
                    if no_escape:
                        evidence['escape'] = ('skipped: step has no '
                                              'foreground command')
                    else:
                        try:
                            evidence['escape'] = self.recovery.escape()
                        except Exception as recovery_exc:
                            evidence['recovery_error'] = str(recovery_exc)
            finally:
                done.set()
                watcher.join()
            if aborted.is_set():
                results[-1].update(state='unknown_outcome', recovery=evidence, observed_result=value, error=error)
                self.store.finish(job['id'], 'unknown_outcome', context, results, str(evidence))
                return True
            if error:
                state = 'unknown_outcome' if uncertain else 'failed'
                self.quarantined = uncertain
                results[-1].update(state=state, error=error)
                self.store.finish(job['id'], state, context, results, error)
                return True
            results[-1].update(state='succeeded', result=value)
            if step.get('bind'):
                context[step['bind']] = value
            self.store.finish(job['id'], 'running', context, results)
        self.store.finish(job['id'], 'succeeded', context, results)
        return True
