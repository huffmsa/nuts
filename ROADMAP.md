# Roadmap

Features planned after 1.0, roughly in priority order. They came out of planning a move of carrier-facing background jobs onto NUTS. Build each one when a real job needs it.

## Rate limits per key

Concurrency limits cap how many jobs for a key run at once. Most vendor APIs limit request rate instead (N per second or per minute), and NUTS can't express that yet.

Plan: a token bucket per key in a Lua script, checked when a job starts, at the same point as the concurrency gate. A job without a token either parks in the key's line (like a concurrency wait) or goes back on the delayed queue for the time until the next token. Configured on the job (`rate_limit_key`, `rate_limit`, `rate_limit_period_seconds`) or per publish, like `concurrency_key`.

## Pause and resume

Stop handing out work for a job name or a concurrency key, for example during a vendor outage, without losing it. Paused jobs stay queued instead of retrying into the dead letter queue.

Plan: a Redis set of paused names and keys, checked when a job starts, in the same place as the cancel check. A paused job is parked or re-delayed, not failed. Add `admin.pause()` and `admin.resume()`, buttons on the Jobs and Concurrency pages, and a `nuts pause` / `nuts resume` command.

## De-duplicated publishes

Workflow instances can be keyed by entity (`entity_key`), but plain jobs can't. A retried API call or a double click can queue the same job twice.

Plan: `publish(..., dedupe_key='sim:42:refresh', dedupe_seconds=300)` does `SET NX EX` before queueing and returns the existing dispatch id if the key is taken. Release the key when the job finishes, so a later publish isn't blocked for the whole window.

## Test helpers for job code

Repositories that use NUTS should be able to unit-test their jobs without writing worker loops by hand.

Plan: `worker.run_until_idle(max_steps=1000)`, which runs leader ticks and jobs until nothing is due, and a pytest plugin with a `nuts_worker` fixture that takes a Redis URL and a list of job modules. Possibly an option that runs delayed jobs and retries immediately, so tests don't sleep.
