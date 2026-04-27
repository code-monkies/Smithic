# Mission — Cross-shell history search

A faster, cross-shell replacement for `Ctrl+R`. Indexes shell history (bash,
zsh, fish, PowerShell) into a single searchable store, ranks by recency +
frequency + directory match, and presents a fuzzy-finder UI that's snappy
enough to use as the default `Ctrl+R` binding in every shell.

## Who it's for

- Developers who switch between bash on Linux servers, zsh on a Mac laptop,
  and PowerShell on a Windows workstation and want one history that follows
  them.
- Anyone who's typed the same `kubectl` invocation a hundred times and is
  tired of `Ctrl+R`'s linear scan.
- Pair-programmers who want to share a history scope across two machines.

## Success looks like

- Sub-50ms search response on a 100k-entry history.
- Atomic shell hooks for bash / zsh / fish / PowerShell installed by a single
  install script, no manual rcfile edits required.
- Optional encrypted sync (end-to-end, user holds the key) between machines
  that share a config.
- 5,000 GitHub stars and packaging in Homebrew / Scoop / nixpkgs within a year.
- A working integration with Atuin / fzf so users coming from those tools
  don't lose existing muscle memory.

## Out of scope

- Cloud-hosted sync. End-to-end is fine; SaaS isn't the point.
- A full-fledged shell. We extend existing shells; we don't replace them.
- Mobile / web UI. CLI-only.
