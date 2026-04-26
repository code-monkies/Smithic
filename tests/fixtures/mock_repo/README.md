# mock_repo

Synthetic target used by Smithic's integration tests. A tiny Python project
with a manifest, a CLAUDE.md, and a deliberate gap (no `/healthz` endpoint) so
tests can ask the swarm to add it and verify the resulting PR shape.
