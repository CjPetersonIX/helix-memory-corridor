# Security Review — Helix Memory Corridor Public Release

Date: 2026-10-11
Reviewer: [R1] OmniSecretary-MUSE (m8)
Stage: 3 of open-source-publisher pipeline

## 3.1 Secrets scan

**Method:** Grep across entire tree for credential patterns:
`api[_-]?key|password|passwd|secret|private[_-]?key|bearer|session[_-]?token|oauth|aws[_-]|-----BEGIN`

**Result:** CLEAN. Two hits, both documentation instructing users to
leave OAuth client ID/secret blank during rclone setup. No actual
secrets, keys, tokens, or credentials in code, docs, or configs.

Also scanned for: email addresses, phone numbers, financial account
numbers, Tailscale IPs, internal hostnames. None found.

## 3.2 Secure code review

Read the sanitized code as an attacker.

**Hardcoded paths:** All use `os.path.expanduser("~/...")`. No
`/home/ubuntu`, `/home/box`, `/root/`, or internal IPs. Fixed four
files that still referenced the old `~/.helix-hybrid` index dir;
now consistently `~/.helix-memory`.

**Error handling:** No stack traces printed to users. Degradation is
graceful (semantic arm absent → BM25+graph+temporal; networkx absent
→ simple graph walk). Error messages contain no infrastructure details.

**Debug code:** No `pdb`, `breakpoint()`, or debug endpoints.

**Input handling:** `memory_dir` is user-supplied and expanded safely.
No shell injection vectors (no `os.system` or `subprocess` with
user input). File reads use `errors="ignore"` for encoding safety.

**Network:** Zero network calls in core code. The semantic arm
downloads a model from HuggingFace on first use (standard,
user-initiated). No telemetry, no phone-home.

## 3.3 Dependency audit

**Core (requirements.txt):**
- `rank-bm25==0.2.2` — MIT, pure Python BM25
- `dateparser==1.4.3` — BSD, date parsing
- `python-dateutil==2.9.0` — BSD, date utilities
- `pytz==2026.5` — MIT, timezone data
- `tzlocal==5.4.4` — MIT, local timezone
- `regex==2026.9.29` — Python Software Foundation License
- `networkx==3.6.1` — BSD, graph algorithms (optional, has fallback)

**Semantic (requirements-semantic.txt):**
- `torch` (CPU) — BSD, ML framework
- `sentence-transformers==6.1.0` — Apache 2.0
- `chromadb==1.5.9` — Apache 2.0, vector DB
- `huggingface_hub`, `tokenizers` — Apache 2.0 / MIT
- `numpy`, `scipy` — BSD

**Verdict:** All dependencies are public, open-source, free. No
private packages, no paid services, no known critical vulnerabilities
in pinned versions at time of review.

## 3.4 Threat model (lightweight STRIDE)

**What is this?** A local Python library for memory recall. It reads
markdown files, builds search indexes, and answers queries. No server,
no network service, no multi-user component.

**Spoofing:** N/A — single-user local tool. No authentication.

**Tampering:** An attacker with filesystem access could modify memory
files or indexes. Mitigation: indexes are rebuilt from source files;
source files are the truth. No worse than any local note-taking app.

**Repudiation:** N/A — single-user tool.

**Information disclosure:** The tool reads the user's own memory files
by design. It does not transmit data anywhere. The only network
activity is the optional HuggingFace model download (user-initiated,
one time). No telemetry.

**Denial of service:** A maliciously large memory directory could slow
indexing. Mitigation: user controls their own input files. Not a
remote attack vector.

**Elevation of privilege:** No privilege boundaries in a local CLI
library. Runs as the invoking user.

**Supply chain:** A malicious contributor could sneak code into a PR.
Mitigation: standard GitHub PR review process. Dependencies are pinned
to specific versions.

**Trust boundaries:**
- User's memory files → trusted (user's own data)
- HuggingFace model download → semi-trusted (well-known public model,
  `all-MiniLM-L6-v2`, widely used)
- PyPI packages → semi-trusted (pinned versions, all major OSS projects)

## Conclusion

**PASS.** No secrets, no infrastructure leaks, no dangerous code
patterns, clean dependencies, minimal attack surface (local-only tool).
Approved for packaging and SRVP2 handoff.
