# Test payloads

`github/` holds example webhook payloads from GitHub's
[octokit/webhooks](https://github.com/octokit/webhooks/tree/main/payload-examples/api.github.com)
(MIT License, Copyright (c) GitHub), unchanged except for the file names
(`<event>.<action>.json`).

To add one, copy the file from there (or from the *Recent Deliveries* page of
a real webhook) and add a test in `tests/test_handlers.py`.
