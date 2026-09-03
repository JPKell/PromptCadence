"""In-process doubles for the services PromptCadence talks to over HTTP.

One lives here: the fake LoadCoach (:mod:`tests.fakes.loadcoach_app`). It is the highest-leverage
artifact of Phase 3 — every phase after it tests against it without a GPU or a live LoadCoach —
so it is held to the same discipline as the code: strict where LoadCoach is strict, stricter in
the one place recovery depends on, and never speaking a response shape LoadCoach does not
actually produce. Every place it differs from the real thing is listed in its module docstring.
"""
